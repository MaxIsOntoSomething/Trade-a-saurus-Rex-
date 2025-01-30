import os
import shutil
import subprocess

# Define the logs directory
logs_dir = "logs"

# Delete all logs if the directory exists
if os.path.exists(logs_dir):
    for file_name in os.listdir(logs_dir):
        file_path = os.path.join(logs_dir, file_name)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            print(f"Failed to delete {file_path}: {e}")

# Start main.py
subprocess.run(["python", "main.py"])
