import os
import sys
import subprocess

def delete_logs():
    logs_folder = os.path.join(os.getcwd(), "logs")
    
    if os.path.exists(logs_folder):
        for file_name in os.listdir(logs_folder):
            file_path = os.path.join(logs_folder, file_name)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.remove(file_path)
                    print(f"Deleted log file: {file_name}")
                elif os.path.isdir(file_path):
                    os.rmdir(file_path)
                    print(f"Deleted log directory: {file_name}")
            except Exception as e:
                print(f"Failed to delete {file_name}: {e}")
    else:
        print("Logs folder does not exist. Skipping deletion.")

def start_main():
    try:
        # Adjust the command to use the correct Python executable
        python_executable = sys.executable
        subprocess.run([python_executable, "main.py"])
    except Exception as e:
        print(f"Failed to start main.py: {e}")

if __name__ == "__main__":
    delete_logs()
    start_main()
