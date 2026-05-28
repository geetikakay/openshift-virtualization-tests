from typing import Any

from ocp_resources.datavolume import DataVolume

from utilities.constants import (
    ARM_64,
    CENTOS_STREAM9_PREFERENCE,
    CENTOS_STREAM10_PREFERENCE,
    EXPECTED_CLUSTER_INSTANCE_TYPE_LABELS,
    HPP_CAPABILITIES,
    OS_FLAVOR_FEDORA,
    PREFERENCE_STR,
    RHEL8_PREFERENCE,
    RHEL9_PREFERENCE,
    RHEL10_PREFERENCE,
    U1_MEDIUM_STR,
    Images,
    StorageClassNames,
)
from utilities.storage import HppCsiStorageClass

global config

Images.Cirros.RAW_IMG_XZ = "cirros-0.4.0-aarch64-disk.raw.xz"
EXPECTED_CLUSTER_INSTANCE_TYPE_LABELS[PREFERENCE_STR] = f"rhel.9.{ARM_64}"


storage_class_matrix = [
    {
        StorageClassNames.TRIDENT_CSI_NFS: {
            "volume_mode": DataVolume.VolumeMode.FILE,
            "access_mode": DataVolume.AccessMode.RWX,
            "snapshot": True,
            "online_resize": True,
            "wffc": False,
        }
    },
    {
        StorageClassNames.IO2_CSI: {
            "volume_mode": DataVolume.VolumeMode.BLOCK,
            "access_mode": DataVolume.AccessMode.RWX,
            "snapshot": True,
            "online_resize": True,
            "wffc": True,
            "default": True,
        }
    },
    {HppCsiStorageClass.Name.HOSTPATH_CSI_BASIC: HPP_CAPABILITIES},
]

storage_class_a = StorageClassNames.IO2_CSI
storage_class_b = StorageClassNames.IO2_CSI

rhel_os_list = ["rhel-9-5", "rhel-9-6"]
fedora_os_list = ["fedora-42"]
centos_os_list = ["centos-stream-9"]

instance_type_rhel_os_list = [RHEL10_PREFERENCE]
instance_type_fedora_os_list = [OS_FLAVOR_FEDORA]
instance_type_centos_os_list = [CENTOS_STREAM10_PREFERENCE]

auto_update_data_source_matrix = [
    {"centos-stream9": {"template_os": "centos-stream9"}},
    {"fedora": {"template_os": "fedora"}},
    {"rhel9": {"template_os": "rhel9.0"}},
]

data_import_cron_matrix = [
    {"centos-stream9": {"instance_type": U1_MEDIUM_STR, "preference": CENTOS_STREAM9_PREFERENCE}},
    {"centos-stream10": {"instance_type": U1_MEDIUM_STR, "preference": CENTOS_STREAM10_PREFERENCE}},
    {"fedora": {"instance_type": U1_MEDIUM_STR, "preference": OS_FLAVOR_FEDORA}},
    {"rhel8": {"instance_type": U1_MEDIUM_STR, "preference": RHEL8_PREFERENCE}},
    {"rhel9": {"instance_type": U1_MEDIUM_STR, "preference": RHEL9_PREFERENCE}},
    {"rhel10": {"instance_type": U1_MEDIUM_STR, "preference": RHEL10_PREFERENCE}},
]

for _dir in dir():
    if not config:
        config: dict[str, Any] = {}
    val = locals()[_dir]
    if type(val) not in [bool, list, dict, str]:
        continue

    if _dir in ["encoding", "py_file"]:
        continue

    config[_dir] = locals()[_dir]
