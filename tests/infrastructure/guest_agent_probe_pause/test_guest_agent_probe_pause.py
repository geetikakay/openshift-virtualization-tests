"""
Guest Agent Probe Pause Tests

Tests for kubevirt.io/pause-guest-agent-probes: validate maintenance workflows with
GuestAgentPing probe suppression, live migration with paused probes on the target,
snapshot coexistence, and stacked VM Pause/Unpause with annotation-based pause.
"""

import logging

import pytest
from ocp_resources.virtual_machine_instance import VirtualMachineInstance
from ocp_resources.virtual_machine_snapshot import VirtualMachineSnapshot

from tests.infrastructure.guest_agent_probe_pause.utils import (
    GUEST_AGENT_PROBE_PAUSE_SSH_KEY_UPDATED_MARKER,
    PAUSE_GUEST_AGENT_PROBES_ANNOTATION,
    POST_MIGRATION_PROBE_KILL_WINDOW_SECONDS,
    assert_vmi_phase_running_for,
    list_vmi_warning_events,
    set_pause_guest_agent_probes,
    update_access_credential_ssh_secret,
    wait_for_access_credentials_sync_failed_guest_agent_offline,
    wait_for_access_credentials_synchronized,
    wait_for_guest_agent_probe_pause_log,
    wait_for_ssh_key_marker_in_guest,
)
from utilities.constants.timeouts import TIMEOUT_15SEC, TIMEOUT_30SEC
from utilities.virt import (
    manage_qemu_guest_agent,
    migrate_vm_and_verify,
    pause_unpause_vm_and_check_connectivity,
)

LOGGER = logging.getLogger(__name__)
GUEST_AGENT_PING_FAILED_REASON = "GuestAgentPingFailed"

pytestmark = pytest.mark.usefixtures("namespace")


class TestGuestAgentProbePauseLifecycle:
    """
    Standard lifecycle and maintenance workflows with kubevirt.io/pause-guest-agent-probes.

    Preconditions:
        - Running VMI with GuestAgentPing probes
        - Guest agent connected before pausing probes
    """

    @pytest.mark.smoke
    def test_full_maintenance_return_to_service(
        self,
        admin_client,
        guest_agent_pause_liveness_readiness_vm,
    ):
        """
        Test complete maintenance workflow with probe pause and return to service. [TS-CNV-82132-002]

        Jira: https://issues.redhat.com/browse/CNV-82132 # <skip-jira-utils-check>

        Preconditions:
            - Under-test VM with GuestAgentPing liveness and readiness probes

        Steps:
            1. Wait until the guest agent is connected and the VMI is Ready
            2. Pause guest-agent probes and stop the guest agent
            3. Confirm the VMI stays Running with no GuestAgentPingFailed events
            4. Start the guest agent and resume guest-agent probes
            5. Confirm the virt-launcher pod UID is unchanged across pause and unpause

        Expected:
            - VMI is Running after probes are resumed
            - The virt-launcher pod was not replaced
        """
        vm = guest_agent_pause_liveness_readiness_vm
        vmi = vm.vmi
        LOGGER.info(f"Wait for guest agent and Ready on {vm.name}")
        vm.wait_for_agent_connected()
        vmi.wait_for_condition(
            condition=VirtualMachineInstance.Condition.READY,
            status=VirtualMachineInstance.Condition.Status.TRUE,
            timeout=TIMEOUT_30SEC,
        )
        launcher_pod_uid = vmi.get_virt_launcher_pod(privileged_client=admin_client).instance.metadata.uid

        LOGGER.info(f"Pause guest-agent probes and stop qemu-guest-agent on {vm.name}")
        set_pause_guest_agent_probes(vmi=vmi, pause=True, admin_client=admin_client)
        baseline_events = list_vmi_warning_events(vmi=vmi)

        manage_qemu_guest_agent(vm=vm, action="stop")
        LOGGER.info(f"Verify {vm.name} stays Running with no GuestAgentPingFailed events")
        assert_vmi_phase_running_for(vmi=vmi, seconds=TIMEOUT_30SEC + TIMEOUT_15SEC)

        baseline_uids = {event.get("metadata", {}).get("uid") for event in baseline_events}
        guest_agent_ping_failures = [
            event
            for event in list_vmi_warning_events(vmi=vmi)
            if event.get("metadata", {}).get("uid") not in baseline_uids
            and event.get("reason") == GUEST_AGENT_PING_FAILED_REASON
        ]
        assert not guest_agent_ping_failures, f"Unexpected GuestAgentPingFailed events: {guest_agent_ping_failures}"

        LOGGER.info(f"Start qemu-guest-agent and resume probes on {vm.name}")
        manage_qemu_guest_agent(vm=vm, action="start")
        set_pause_guest_agent_probes(vmi=vmi, pause=False, admin_client=admin_client)
        vmi.wait_for_condition(
            condition=VirtualMachineInstance.Condition.READY,
            status=VirtualMachineInstance.Condition.Status.TRUE,
            timeout=TIMEOUT_30SEC,
        )
        assert vmi.status == VirtualMachineInstance.Status.RUNNING, (
            f"VMI {vmi.name} phase is {vmi.status}, expected Running"
        )
        assert vmi.get_virt_launcher_pod(privileged_client=admin_client).instance.metadata.uid == launcher_pod_uid, (
            f"virt-launcher pod for {vmi.name} was replaced across pause/unpause"
        )

    def test_annotation_pause_through_vm_pause_cycle(
        self,
        admin_client,
        guest_agent_pause_liveness_vm,
    ):
        """
        Test that annotation probe pause remains effective through VM Pause and Unpause. [TS-CNV-82132-015]

        Jira: https://issues.redhat.com/browse/CNV-82132 # <skip-jira-utils-check>

        Steps:
            1. Wait until the guest agent is connected
            2. Pause guest-agent probes, then pause and unpause the VM
            3. Stop the guest agent and confirm the VMI stays Running
            4. Start the guest agent and resume guest-agent probes

        Expected:
            - VMI is Running and the guest agent is connected after probes are resumed
        """
        vm = guest_agent_pause_liveness_vm
        vmi = vm.vmi
        LOGGER.info(f"Wait for guest agent on {vm.name}")
        vm.wait_for_agent_connected()

        LOGGER.info(f"Pause guest-agent probes, then pause and unpause {vm.name}")
        set_pause_guest_agent_probes(vmi=vmi, pause=True, admin_client=admin_client)
        pause_unpause_vm_and_check_connectivity(vm=vm)
        assert vmi.status == VirtualMachineInstance.Status.RUNNING, (
            f"VMI {vmi.name} phase is {vmi.status} after VM unpause, expected Running"
        )

        LOGGER.info(f"Stop qemu-guest-agent and verify {vm.name} stays Running")
        manage_qemu_guest_agent(vm=vm, action="stop")
        assert_vmi_phase_running_for(vmi=vmi, seconds=TIMEOUT_30SEC + TIMEOUT_15SEC)

        LOGGER.info(f"Start qemu-guest-agent and resume probes on {vm.name}")
        manage_qemu_guest_agent(vm=vm, action="start")
        set_pause_guest_agent_probes(vmi=vmi, pause=False, admin_client=admin_client)
        vm.wait_for_agent_connected()
        assert vmi.status == VirtualMachineInstance.Status.RUNNING, (
            f"VMI {vmi.name} phase is {vmi.status}, expected Running"
        )


@pytest.mark.special_infra
@pytest.mark.rwx_default_storage
class TestGuestAgentProbePauseMigration:
    """
    Live migration plus maintenance on PVC-backed workloads with probe pause.

    Preconditions:
        - Multi-node cluster with migratable RWX storage
        - Guest agent connected before migration
    """

    def test_migrated_maintenance_on_target(
        self,
        admin_client,
        guest_agent_pause_migratable_vm,
    ):
        """
        Test maintenance on a live-migrated PVC-backed workload with probes paused on target. [TS-CNV-82132-007]

        Jira: https://issues.redhat.com/browse/CNV-82132 # <skip-jira-utils-check>

        Steps:
            1. Wait until the guest agent is connected
            2. Pause guest-agent probes and stop the guest agent
            3. Live-migrate the VM and confirm guest-agent probes stay paused on the target launcher
            4. Confirm the VMI stays Running, then start the guest agent and resume probes

        Expected:
            - VMI is Running and the guest agent is connected on the target after probes are resumed
        """
        vm = guest_agent_pause_migratable_vm
        vmi = vm.vmi
        LOGGER.info(f"Wait for guest agent on {vm.name}")
        vm.wait_for_agent_connected()

        LOGGER.info(f"Pause guest-agent probes and stop qemu-guest-agent on {vm.name}")
        set_pause_guest_agent_probes(vmi=vmi, pause=True, admin_client=admin_client)
        manage_qemu_guest_agent(vm=vm, action="stop")

        LOGGER.info(f"Live-migrate {vm.name} and verify probes stay paused on the target")
        migrate_vm_and_verify(
            vm=vm,
            client=admin_client,
            check_ssh_connectivity=False,
            wait_for_interfaces=False,
        )
        wait_for_guest_agent_probe_pause_log(vmi=vm.vmi, admin_client=admin_client)

        LOGGER.info(f"Verify {vm.name} stays Running, then resume probes")
        assert_vmi_phase_running_for(vmi=vm.vmi, seconds=POST_MIGRATION_PROBE_KILL_WINDOW_SECONDS)

        manage_qemu_guest_agent(vm=vm, action="start")
        set_pause_guest_agent_probes(vmi=vm.vmi, pause=False, admin_client=admin_client)
        vm.wait_for_agent_connected()
        assert vm.vmi.status == VirtualMachineInstance.Status.RUNNING, (
            f"VMI {vm.vmi.name} phase is {vm.vmi.status} after migration, expected Running"
        )


@pytest.mark.usefixtures("skip_if_no_storage_class_for_snapshot")
class TestGuestAgentProbePauseSnapshot:
    """
    Snapshot operations coexist with annotation-based probe pause.

    Preconditions:
        - VirtualMachineSnapshot supported for the test VM storage profile
        - Running VMI with guest agent connected
    """

    def test_snapshot_with_pause_annotation(
        self,
        admin_client,
        unprivileged_client,
        guest_agent_pause_snapshot_vm,
    ):
        """
        Test that a VM snapshot completes while the pause annotation is set. [TS-CNV-82132-014]

        Jira: https://issues.redhat.com/browse/CNV-82132 # <skip-jira-utils-check>

        Steps:
            1. Wait until the guest agent is connected
            2. Pause guest-agent probes
            3. Create a VM snapshot and wait until it completes

        Expected:
            - VMI is Running and the pause-guest-agent-probes annotation is still set
        """
        vm = guest_agent_pause_snapshot_vm
        vmi = vm.vmi
        LOGGER.info(f"Wait for guest agent on {vm.name}")
        vm.wait_for_agent_connected()

        LOGGER.info(f"Pause guest-agent probes and snapshot {vm.name}")
        set_pause_guest_agent_probes(vmi=vmi, pause=True, admin_client=admin_client)
        with VirtualMachineSnapshot(
            client=unprivileged_client,
            name=f"{vm.name}-snapshot",
            namespace=vm.namespace,
            vm_name=vm.name,
        ) as snapshot:
            snapshot.wait_snapshot_done()

        assert vmi.status == VirtualMachineInstance.Status.RUNNING, (
            f"VMI {vmi.name} phase is {vmi.status} after snapshot, expected Running"
        )
        assert vmi.instance.metadata.annotations[PAUSE_GUEST_AGENT_PROBES_ANNOTATION] == "true", (
            f"VMI {vmi.name} pause-guest-agent-probes annotation is not true after snapshot"
        )


class TestGuestAgentProbePauseAccessCredentials:
    """
    qemuGuestAgent access credential sync while GuestAgentPing probes are paused.

    Preconditions:
        - VM created with qemuGuestAgent accessCredentials so virt-launcher mounts the secret
        - Guest agent connected before pausing probes
    """

    def test_access_credential_injection_while_paused_and_agent_connected(
        self,
        admin_client,
        guest_agent_pause_access_cred_connected_vm,
        guest_agent_pause_ssh_secret,
    ):
        """
        Test qemuGuestAgent access credential reload succeeds while probes are paused and the agent is connected. [TS-CNV-82132-010]

        Jira: https://issues.redhat.com/browse/CNV-82132 # <skip-jira-utils-check>

        Steps:
            1. Wait until the guest agent is connected, the VMI is Ready, and AccessCredentialsSynchronized=True
            2. Pause guest-agent probes
            3. Update the mounted SSH secret to force a virt-launcher credential reload
            4. Confirm the updated key is present in the guest authorized_keys

        Expected:
            - Credential reload succeeds via guest-ping while probes remain paused
            - VMI stays Running and Ready
        """
        vm = guest_agent_pause_access_cred_connected_vm
        vmi = vm.vmi
        username = vm.username
        assert username, f"Could not determine guest username for {vm.name}"

        LOGGER.info(f"Wait for guest agent, Ready, and credential sync on {vm.name}")
        vm.wait_for_agent_connected()
        vmi.wait_for_condition(
            condition=VirtualMachineInstance.Condition.READY,
            status=VirtualMachineInstance.Condition.Status.TRUE,
            timeout=TIMEOUT_30SEC,
        )
        wait_for_access_credentials_synchronized(vmi=vmi)

        LOGGER.info(f"Pause guest-agent probes and update the SSH secret on {vm.name}")
        set_pause_guest_agent_probes(vmi=vmi, pause=True, admin_client=admin_client)
        update_access_credential_ssh_secret(
            secret=guest_agent_pause_ssh_secret,
            marker=GUEST_AGENT_PROBE_PAUSE_SSH_KEY_UPDATED_MARKER,
        )
        wait_for_ssh_key_marker_in_guest(
            vm=vm,
            username=username,
            marker=GUEST_AGENT_PROBE_PAUSE_SSH_KEY_UPDATED_MARKER,
        )

        assert vmi.status == VirtualMachineInstance.Status.RUNNING, (
            f"VMI {vmi.name} phase is {vmi.status}, expected Running"
        )
        vmi.wait_for_condition(
            condition=VirtualMachineInstance.Condition.READY,
            status=VirtualMachineInstance.Condition.Status.TRUE,
            timeout=TIMEOUT_30SEC,
        )

    def test_access_credential_injection_fails_when_agent_stopped_while_paused(
        self,
        admin_client,
        guest_agent_pause_access_cred_offline_vm,
        guest_agent_pause_ssh_secret,
    ):
        """
        Test qemuGuestAgent access credential reload fails when the guest agent is stopped. [TS-CNV-82132-011]

        Jira: https://issues.redhat.com/browse/CNV-82132 # <skip-jira-utils-check>

        Steps:
            1. Wait until the guest agent is connected and AccessCredentialsSynchronized=True
            2. Pause guest-agent probes and stop the guest agent
            3. Update the mounted SSH secret to force a virt-launcher credential reload
            4. Wait for AccessCredentialsSynchronized=False with message "Guest agent is offline"
            5. Confirm the VMI stays Running while probes remain paused

        Expected:
            - AccessCredentialsSynchronized=False with message containing Guest agent is offline
            - VMI stays Running with probe pause active; same agent-down sync failure as without the annotation
        """
        vm = guest_agent_pause_access_cred_offline_vm
        vmi = vm.vmi

        LOGGER.info(f"Wait for guest agent and credential sync on {vm.name}")
        vm.wait_for_agent_connected()
        wait_for_access_credentials_synchronized(vmi=vmi)

        LOGGER.info(f"Pause guest-agent probes and stop qemu-guest-agent on {vm.name}")
        set_pause_guest_agent_probes(vmi=vmi, pause=True, admin_client=admin_client)
        manage_qemu_guest_agent(vm=vm, action="stop")

        LOGGER.info(f"Update the SSH secret to force credential reload while agent is down on {vm.name}")
        update_access_credential_ssh_secret(
            secret=guest_agent_pause_ssh_secret,
            marker=GUEST_AGENT_PROBE_PAUSE_SSH_KEY_UPDATED_MARKER,
        )
        wait_for_access_credentials_sync_failed_guest_agent_offline(vmi=vmi)

        LOGGER.info(f"Verify {vm.name} stays Running with probes paused and agent unavailable")
        assert_vmi_phase_running_for(vmi=vmi, seconds=TIMEOUT_30SEC + TIMEOUT_15SEC)
        agent_conditions = [
            condition
            for condition in vmi.instance.status.conditions
            if condition["type"] == "AgentConnected"
        ]
        assert not agent_conditions or all(condition.get("status") != "True" for condition in agent_conditions), (
            f"VMI {vmi.name} unexpectedly reports AgentConnected=True while guest agent is stopped"
        )
