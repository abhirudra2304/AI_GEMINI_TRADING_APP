import sys
import os

def ensure_venv():
    """
    Checks if the script is running in a virtual environment.
    If not, it attempts to find a venv and re-run the script within it.
    This should be called at the very top of an entry-point script.
    """
    # In a virtual environment, sys.prefix points to the venv directory,
    # and sys.base_prefix points to the global Python installation.
    # If they are the same, we are NOT in a virtual environment.
    if sys.prefix == sys.base_prefix:
        # sys.argv[0] is the script being run. We want its directory.
        # The project root is assumed to be the directory containing the script.
        project_root = os.path.dirname(os.path.abspath(sys.argv[0]))

        # Common venv directory names
        venv_dirs = ['venv', '.venv']

        # Platform-specific path to the Python executable within the venv
        if sys.platform == "win32":
            python_executable_path = os.path.join('Scripts', 'python.exe')
        else:
            python_executable_path = os.path.join('bin', 'python')

        venv_python = None
        for venv_dir in venv_dirs:
            potential_path = os.path.join(project_root, venv_dir, python_executable_path)
            if os.path.isfile(potential_path):
                venv_python = potential_path
                break

        if venv_python:
            print(f"--- Not in a virtual environment. Relaunching with {venv_python} ---")
            try:
                os.execv(venv_python, [venv_python] + sys.argv)
            except Exception as e:
                print(f"--- FATAL: Failed to relaunch in virtual environment: {e} ---")
                sys.exit(1)
        else:
            print("--- WARNING: No virtual environment found in project root. ---")
            print("--- Running with the global Python interpreter. This is not recommended. ---")
            print("--- To create a venv, run: `python -m venv venv` ---")