"""Helpers for guest agent probe pause tests (CNV-82132)."""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from kubernetes.dynamic import DynamicClient
from ocp_resources.data_source import DataSource
from ocp_resources.event import Event
from ocp_resources.namespace import Namespace
from ocp_resources.resource import ResourceEditor
from ocp_resources.secret import Secret
from ocp_resources.virtual_machine_instance import VirtualMachineInstance
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from utilities.constants.images import OS_FLAVOR_RHEL
from utilities.constants.timeouts import (
    TIMEOUT_5SEC,
    TIMEOUT_10SEC,
    TIMEOUT_15SEC,
    TIMEOUT_30SEC,
    TIMEOUT_2MIN,
)
from utilities.data_utils import base64_encode_str
from utilities.storage import data_volume_template_with_source_ref_dict
from utilities.virt import VirtualMachineForTests, run_os_command, running_vm

LOGGER = logging.getLogger(__name__)

PAUSE_GUEST_AGENT_PROBES_ANNOTATION = "kubevirt.io/pause-guest-agent-probes"
SHORT_PROBE_FAIL_THRESHOLD = 3
POST_MIGRATION_PROBE_KILL_WINDOW_SECONDS = TIMEOUT_30SEC + SHORT_PROBE_FAIL_THRESHOLD * TIMEOUT_5SEC + TIMEOUT_15SEC


@contextmanager
def running_pvc_vm_with_guest_agent_ping_probes(
    name: str,
    namespace: Namespace,
    client: DynamicClient,
    data_source: DataSource,
    storage_class: str | None = None,
    liveness: bool = True,
    readiness: bool = False,
    eviction_strategy: str | None = None,
    access_credential_secret: Secret | None = None,
) -> Generator[VirtualMachineForTests]:
    """Create a running PVC-backed VM with GuestAgentPing probes.

    The probes are patched onto spec.template.spec (sibling of domain) before the VM starts.
    VirtualMachineForTests builds the PVC spec during create, so the probes cannot be stored on a caller body.
    qemuGuestAgent accessCredentials must be on the VM before start so virt-launcher mounts the secret
    and starts the credential watcher in preStartHook.

    Args:
        name: VirtualMachine name.
        namespace: Namespace for the VM.
        client: Client used to create the VM.
        data_source: Golden image DataSource cloned into the VM disk.
        storage_class: Storage class for the VM disk. If None, the cluster default is used.
        liveness: Whether to configure a GuestAgentPing liveness probe.
        readiness: Whether to configure a GuestAgentPing readiness probe.
        eviction_strategy: Eviction strategy passed to VirtualMachineForTests.
        access_credential_secret: Optional SSH public-key Secret mounted for qemuGuestAgent accessCredentials.

    Yields:
        Running VirtualMachineForTests instance.
    """
    LOGGER.info(f"Creating running PVC VM {name} with GuestAgentPing probes in {namespace.name}")
    with VirtualMachineForTests(
        name=name,
        namespace=namespace.name,
        client=client,
        os_flavor=OS_FLAVOR_RHEL,
        vm_instance_type_infer=True,
        vm_preference_infer=True,
        eviction_strategy=eviction_strategy,
        data_volume_template=data_volume_template_with_source_ref_dict(
            data_source=data_source,
            storage_class=storage_class,
        ),
    ) as vm:
        patches: dict[str, Any] = {}
        if liveness:
            patches["livenessProbe"] = {
                "guestAgentPing": {},
                "periodSeconds": TIMEOUT_5SEC,
                "initialDelaySeconds": TIMEOUT_30SEC,
                "failureThreshold": SHORT_PROBE_FAIL_THRESHOLD,
            }
        if readiness:
            patches["readinessProbe"] = {
                "guestAgentPing": {},
                "periodSeconds": TIMEOUT_5SEC,
                "initialDelaySeconds": TIMEOUT_10SEC,
            }
        if access_credential_secret:
            vm.set_login_params()
            assert vm.username, f"Could not determine guest username for qemuGuestAgent accessCredentials on {vm.name}"
            patches["accessCredentials"] = [
                qemu_guest_agent_ssh_access_credential(
                    secret_name=access_credential_secret.name,
                    username=vm.username,
                )
            ]
        if patches:
            ResourceEditor(
                patches={
                    vm: {
                        "spec": {
                            "template": {
                                "spec": patches,
                            }
                        }
                    }
                }
            ).update()
        running_vm(vm=vm)
        yield vm


def set_pause_guest_agent_probes(
    vmi: VirtualMachineInstance,
    pause: bool,
    admin_client: DynamicClient,
) -> None:
    """
    Set or remove the guest agent probe pause annotation.

    Args:
        vmi (VirtualMachineInstance): The VMI to patch.
        pause (bool): True to add the annotation, False to remove it.
        admin_client (DynamicClient): Cluster admin client for reading virt-launcher logs.
    """
    ResourceEditor(
        patches={vmi: {"metadata": {"annotations": {PAUSE_GUEST_AGENT_PROBES_ANNOTATION: "true" if pause else None}}}}
    ).update()
    if pause:
        wait_for_guest_agent_probe_pause_log(vmi=vmi, admin_client=admin_client)


def wait_for_guest_agent_probe_pause_log(
    vmi: VirtualMachineInstance,
    admin_client: DynamicClient,
    timeout: int = TIMEOUT_30SEC,
) -> None:
    """Wait until the current virt-launcher compute log reports probe pause.

    After live migration this reads the target virt-launcher pod. The annotation is already
    on the VMI, so this does not patch it again.

    Args:
        vmi: VMI whose virt-launcher pod to read.
        admin_client: Cluster admin client for reading logs.
        timeout: Maximum time to wait in seconds.

    Raises:
        TimeoutExpiredError: If the pause log is not present within the timeout.
    """
    error_message = f"virt-launcher for {vmi.name} did not log guest agent probe pause"
    pod = vmi.get_virt_launcher_pod(privileged_client=admin_client)
    sampler = TimeoutSampler(
        wait_timeout=timeout,
        sleep=2,
        func=lambda: pod.log(container="compute"),
    )
    try:
        for logs in sampler:
            if "Guest agent probe pause state changed: paused=true" in logs:
                return
    except TimeoutExpiredError as timeout_error:
        raise TimeoutExpiredError(error_message) from timeout_error
    raise TimeoutExpiredError(error_message)


def list_vmi_warning_events(vmi: VirtualMachineInstance) -> list[dict[str, Any]]:
    """
    List recent Warning events for a VirtualMachineInstance.

    Uses a list call rather than ``vmi.events()``. The watch API yields dict envelopes, not
    resource objects. ``Event.list`` only includes events from the last five minutes.

    Args:
        vmi (VirtualMachineInstance): The VMI to get events for.

    Returns:
        list[dict[str, Any]]: Warning event dictionaries for this VMI.
    """
    return [
        event.to_dict()
        for event in Event.list(
            client=vmi.client,
            namespace=vmi.namespace,
            field_selector=f"involvedObject.name=={vmi.name},type==Warning",
        )
    ]


ACCESS_CREDENTIALS_SYNCHRONIZED = "AccessCredentialsSynchronized"
GUEST_AGENT_OFFLINE_ACCESS_CREDENTIAL_MESSAGE = "Guest agent is offline"
GUEST_AGENT_PROBE_PAUSE_SSH_KEY_MARKER = "cnv-82132-probe-pause-access-cred"
GUEST_AGENT_PROBE_PAUSE_SSH_KEY_UPDATED_MARKER = "cnv-82132-probe-pause-access-cred-updated"
GUEST_AGENT_PROBE_PAUSE_SSH_SECRET_KEY = "id_rsa.pub"


def qemu_guest_agent_ssh_public_key(marker: str) -> str:
    """Return a dummy SSH public key whose comment encodes a test marker.

    Args:
        marker: Comment string grepped from authorized_keys.

    Returns:
        SSH public key line.
    """
    return f"ssh-rsa AAAAB3NzaC1yc2EAAAABIwAAAQEA6NF8iallvQVp22WDkT {marker}"


def qemu_guest_agent_ssh_access_credential(secret_name: str, username: str) -> dict[str, Any]:
    """Build an accessCredentials entry that injects an SSH key via qemu-guest-agent.

    Args:
        secret_name: Secret that holds the SSH public key.
        username: Guest user that should receive the key.

    Returns:
        dict[str, Any]: One accessCredentials entry for a VMI or VM template spec.
    """
    return {
        "sshPublicKey": {
            "source": {"secret": {"secretName": secret_name}},
            "propagationMethod": {"qemuGuestAgent": {"users": [username]}},
        }
    }


def update_access_credential_ssh_secret(secret: Secret, marker: str) -> None:
    """Update the mounted SSH secret so virt-launcher reloads qemuGuestAgent credentials.

    virt-launcher watches the secret volume with fsnotify. Adding accessCredentials after
    domain start does not start the watcher or mount the secret (preStartHook only).

    Args:
        secret: Secret already referenced by the VMI accessCredentials and mounted on virt-launcher.
        marker: Comment string written into the dummy public key.
    """
    ResourceEditor(
        patches={
            secret: {
                "data": {
                    GUEST_AGENT_PROBE_PAUSE_SSH_SECRET_KEY: base64_encode_str(
                        text=qemu_guest_agent_ssh_public_key(marker)
                    )
                }
            }
        }
    ).update()


def wait_for_ssh_key_marker_in_guest(
    vm: VirtualMachineForTests,
    username: str,
    marker: str,
    timeout: int = TIMEOUT_2MIN,
) -> None:
    """Wait until the guest authorized_keys file contains the SSH key marker.

    Args:
        vm: Running VM with SSH access.
        username: Guest user whose authorized_keys to read.
        marker: Marker previously written into the SSH secret.
        timeout: Maximum time to wait in seconds.

    Raises:
        TimeoutExpiredError: If the marker is not present in time.
    """
    error_message = f"SSH key marker {marker} not found in authorized_keys for {vm.name}"
    sampler = TimeoutSampler(
        wait_timeout=timeout,
        sleep=5,
        func=run_os_command,
        func_kwargs={
            "vm": vm,
            "command": f"grep -F {marker} /home/{username}/.ssh/authorized_keys",
        },
        exceptions_dict={Exception: []},
    )
    try:
        for authorized_keys in sampler:
            if authorized_keys and marker in authorized_keys:
                return
    except TimeoutExpiredError as timeout_error:
        raise TimeoutExpiredError(error_message) from timeout_error
    raise TimeoutExpiredError(error_message)


def wait_for_access_credentials_synchronized(
    vmi: VirtualMachineInstance,
    timeout: int = TIMEOUT_2MIN,
) -> None:
    """Wait until the VMI reports AccessCredentialsSynchronized=True.

    Args:
        vmi: VMI that should synchronize access credentials.
        timeout: Maximum time to wait in seconds.
    """
    vmi.wait_for_condition(
        condition=ACCESS_CREDENTIALS_SYNCHRONIZED,
        status=VirtualMachineInstance.Condition.Status.TRUE,
        timeout=timeout,
    )


def wait_for_access_credentials_sync_failed_guest_agent_offline(
    vmi: VirtualMachineInstance,
    timeout: int = TIMEOUT_2MIN,
) -> None:
    """Wait until virt-handler reports failed qemuGuestAgent credential sync.

    Matches kubevirt/pkg/virt-launcher/virtwrap/access-credentials/access_credentials.go
    (reloadCredentialFiles: guest-ping failure -> Succeeded=false, message "Guest agent is offline")
    and pkg/virt-handler/vm.go (maps launcher metadata to AccessCredentialsSynchronized=False).

    Args:
        vmi: VMI that should report credential sync failure while the guest agent is down.
        timeout: Maximum time to wait in seconds.

    Raises:
        TimeoutExpiredError: If the expected condition is not observed in time.
    """
    vmi.wait_for_condition(
        condition=ACCESS_CREDENTIALS_SYNCHRONIZED,
        status=VirtualMachineInstance.Condition.Status.FALSE,
        timeout=timeout,
    )
    message = next(
        (
            condition.get("message", "")
            for condition in vmi.instance.status.conditions
            if condition["type"] == ACCESS_CREDENTIALS_SYNCHRONIZED
        ),
        "",
    )
    assert GUEST_AGENT_OFFLINE_ACCESS_CREDENTIAL_MESSAGE in message, (
        f"VMI {vmi.name} {ACCESS_CREDENTIALS_SYNCHRONIZED} message is {message!r}, "
        f"expected to contain {GUEST_AGENT_OFFLINE_ACCESS_CREDENTIAL_MESSAGE!r}"
    )


def assert_vmi_phase_running_for(vmi: VirtualMachineInstance, seconds: int) -> None:
    """
    Assert that a VMI remains in the Running phase for a specified duration.

    TimeoutSampler catches exceptions raised by the caller of its generator, including
    assertion failures. Failed samples are recorded here and checked after the generator
    stops. API errors are not retried.

    Args:
        vmi (VirtualMachineInstance): The VMI to check.
        seconds (int): The duration to monitor the VMI in seconds.

    Raises:
        TimeoutExpiredError: If a sample raises or no sample is read before the timeout.
    """
    observed = False
    failed = False
    sampler = TimeoutSampler(
        wait_timeout=seconds,
        sleep=2,
        func=lambda: vmi.status == VirtualMachineInstance.Status.RUNNING,
        exceptions_dict={},
    )
    try:
        for sample in sampler:
            observed = True
            if not sample:
                failed = True
                break
    except TimeoutExpiredError as timeout_error:
        if timeout_error.last_exp is not None or not observed:
            raise
    assert observed and not failed, f"VMI {vmi.name} phase is {vmi.status}, expected Running"
