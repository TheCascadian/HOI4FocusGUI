# region File (auto-generated)
# endregion

from _imports import (
    # Standard library
    os, shutil, sys,
)

from datetime import datetime


def take_backup(src_file_name, dst_file_name=None, src_dir='', dst_dir=''):
    try:
        # Handle missing source file name
        if not src_file_name:
            print("Please provide the Source File Name.")
            return

        # Get current timestamp for unique backup naming
        now = datetime.now()
        timestamp = now.strftime("%d_%b_%Y_%H-%M-%S_")

        # Construct full source path
        src_path = os.path.join(src_dir, src_file_name)

        # Determine destination file name
        if not dst_file_name or dst_file_name.isspace():
            dst_file_name = src_file_name

        # Construct full destination path with timestamp
        dst_path = os.path.join(dst_dir, f"{timestamp}{dst_file_name}")

        # Perform the backup
        if os.path.isfile(src_path):
            shutil.copy2(src_path, dst_path)
            print(f"File Backup Successful: {dst_path}")
        elif os.path.isdir(src_path):
            shutil.copytree(src_path, dst_path)
            print(f"Folder Backup Successful: {dst_path}")
        else:
            print("Source does not exist. Please check the path.")
    except FileNotFoundError:
        print("File or folder not found! Provide a valid path.")
    except PermissionError:
        print("Permission denied! Check your access rights.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

# Example usage
take_backup("_focusGUI.py", dst_dir="backup_folder/")