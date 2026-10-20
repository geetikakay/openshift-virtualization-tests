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
from ocp_resources.virtual_machine_instance import VirtualMachineInstance
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from utilities.constants.images import OS_FLAVOR_RHEL
from utilities.constants.timeouts import (
    TIMEOUT_5SEC,
    TIMEOUT_10SEC,
    TIMEOUT_15SEC,
    TIMEOUT_30SEC,
)
from utilities.storage import data_volume_template_with_source_ref_dict
from utilities.virt import VirtualMachineForTests, running_vm

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
) -> Generator[VirtualMachineForTests]:
    """Create a running PVC-backed VM with GuestAgentPing probes.

    The probes are patched onto spec.template.spec (sibling of domain) before the VM starts.
    VirtualMachineForTests builds the PVC spec during create, so the probes cannot be stored on a caller body.

    Args:
        name: VirtualMachine name.
        namespace: Namespace for the VM.
        client: Client used to create the VM.
        data_source: Golden image DataSource cloned into the VM disk.
        storage_class: Storage class for the VM disk. If None, the cluster default is used.
        liveness: Whether to configure a GuestAgentPing liveness probe.
        readiness: Whether to configure a GuestAgentPing readiness probe.
        eviction_strategy: Eviction strategy passed to VirtualMachineForTests.

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
