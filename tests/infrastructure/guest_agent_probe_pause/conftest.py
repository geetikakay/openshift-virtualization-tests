import pytest
from ocp_resources.datavolume import DataVolume
from ocp_resources.storage_profile import StorageProfile

from tests.infrastructure.guest_agent_probe_pause.utils import running_pvc_vm_with_guest_agent_ping_probes
from utilities.constants.virt import LIVE_MIGRATE


@pytest.fixture()
def guest_agent_pause_liveness_readiness_vm(
    unprivileged_client,
    latest_rhel_data_source,
    namespace,
):
    with running_pvc_vm_with_guest_agent_ping_probes(
        name="gap-liveness-readiness",
        namespace=namespace,
        client=unprivileged_client,
        data_source=latest_rhel_data_source,
        readiness=True,
    ) as vm:
        yield vm


@pytest.fixture()
def guest_agent_pause_liveness_vm(
    unprivileged_client,
    latest_rhel_data_source,
    namespace,
):
    with running_pvc_vm_with_guest_agent_ping_probes(
        name="gap-liveness",
        namespace=namespace,
        client=unprivileged_client,
        data_source=latest_rhel_data_source,
    ) as vm:
        yield vm


@pytest.fixture(scope="module")
def guest_agent_pause_rwx_storage_class(unprivileged_client, available_storage_classes_names):
    for storage_class_name in available_storage_classes_names:
        access_modes = StorageProfile(
            client=unprivileged_client,
            name=storage_class_name,
        ).first_claim_property_set_access_modes()
        if access_modes and access_modes[0] == DataVolume.AccessMode.RWX:
            return storage_class_name
    pytest.fail("No RWX storage class available for migratable guest-agent probe pause test")


@pytest.fixture()
def guest_agent_pause_migratable_vm(
    unprivileged_client,
    latest_rhel_data_source,
    namespace,
    guest_agent_pause_rwx_storage_class,
):
    with running_pvc_vm_with_guest_agent_ping_probes(
        name="gap-migrate",
        namespace=namespace,
        client=unprivileged_client,
        data_source=latest_rhel_data_source,
        storage_class=guest_agent_pause_rwx_storage_class,
        eviction_strategy=LIVE_MIGRATE,
    ) as vm:
        yield vm


@pytest.fixture()
def guest_agent_pause_snapshot_vm(
    unprivileged_client,
    storage_class_for_snapshot,
    latest_rhel_data_source,
    namespace,
):
    with running_pvc_vm_with_guest_agent_ping_probes(
        name="gap-snapshot",
        namespace=namespace,
        client=unprivileged_client,
        data_source=latest_rhel_data_source,
        storage_class=storage_class_for_snapshot,
    ) as vm:
        yield vm
