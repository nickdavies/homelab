#!/usr/bin/env python3
import yaml
import os
import argparse
from typing import Any


def load_yaml_file(filepath: str) -> dict[str, Any]:
    """Load YAML file and return parsed content, or empty dict if file doesn't exist"""
    if not os.path.exists(filepath):
        return {}

    try:
        with open(filepath, "r") as f:
            content = yaml.safe_load(f)
            return content if content is not None else {}
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        return {}


def save_yaml_file(filepath: str, data: dict[str, Any]):
    """Save data to YAML file"""
    try:
        with open(filepath, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
        print(f"Successfully updated {filepath}")
    except Exception as e:
        print(f"Error saving {filepath}: {e}")


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Clean up duplicate devices from zigbee2mqtt device files"
    )
    _ = parser.add_argument(
        "--devices-auto-path",
        required=True,
        type=str,
        help="Path to the devices-auto.yaml file",
    )
    _ = parser.add_argument(
        "--devices-static-path",
        required=True,
        type=str,
        help="Path to the devices.yaml file",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    devices_auto_path = args.devices_auto_path
    devices_static_path = args.devices_static_path

    print("Loading device configuration files...")

    # Load both files
    devices_auto = load_yaml_file(devices_auto_path)
    devices_static = load_yaml_file(devices_static_path)

    print(f"Found {len(devices_auto)} devices in devices-auto.yaml")
    print(f"Found {len(devices_static)} devices in devices.yaml")

    # Find devices that exist in both files
    duplicates = set(devices_auto.keys()) & set(devices_static.keys())

    if duplicates:
        print(
            f"Found {len(duplicates)} duplicate devices to remove from devices-auto.yaml:"
        )
        for device_id in duplicates:
            print(f"  - {device_id}")
            del devices_auto[device_id]

        # Save the cleaned devices-auto.yaml
        save_yaml_file(devices_auto_path, devices_auto)
        print(f"Removed {len(duplicates)} duplicate devices from devices-auto.yaml")
    else:
        print("No duplicate devices found, no changes needed")


if __name__ == "__main__":
    main()
