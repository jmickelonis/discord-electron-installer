#!/bin/python3

import sys

_MIN_PYTHON_VERSION = 10


def main():
    version_info = sys.version_info
    if version_info.major != 3 or version_info.minor < _MIN_PYTHON_VERSION:
        print(f'Python 3.{_MIN_PYTHON_VERSION}+ required!')
        exit()

    print(f'Python version: 3.{version_info.minor}.{version_info.micro}')

    from pathlib import Path
    from shutil import rmtree
    from subprocess import run

    root = Path(__file__).parent.absolute()
    venv = root / '.venv'

    if venv.exists():
        rmtree(venv)

    print('Creating virtual environment...')
    run([sys.executable, '-m', 'venv', '.venv'], check=True)

    print('Installing dependencies...')
    run([venv / 'bin' / 'pip', 'install', '-r', 'requirements.txt'], check=True)

    file = Path('run.py').absolute()
    run(['chmod', '+x', file], check=True)
    print(f'Setup finished! To run, execute {file}')


if __name__ == '__main__':
    main()
