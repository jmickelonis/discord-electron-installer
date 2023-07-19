#!.venv/bin/python

import json
import os
import re
from pathlib import Path
from shutil import copy, copytree, rmtree
from subprocess import run

import click
import requests

_INSTALLED_VERSION_PATTERN = re.compile(r'^(\S+)/\S+ (\S+) ')


def apt_get_installed_version(name: str) -> str | None:
    versions = apt_get_installed_versions(name)
    return versions.get(name)


def apt_get_installed_versions(*names: str) -> dict[str, str]:
    d = {}
    result = run(['apt', 'list', *names, '--installed'], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if match := _INSTALLED_VERSION_PATTERN.match(line):
            d[match[1]] = match[2]
    return d


def npm_get_latest_version(name: str) -> str:
    result = run(
        ['npm', '-g', 'view', name, 'version'],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def npm_install(name: str, version: str | None = None):
    if version:
        run(['sudo', 'npm', '-g', 'install', f'{name}@{version}'], check=True)
        return

    latest_version = npm_get_latest_version(name)

    result = run(
        ['npm', '-g', '--json', 'list', name],
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    dependencies = data.get('dependencies')

    if not dependencies:
        if not click.confirm(
            f'npm - {name} not found! Install {name} v{latest_version} (required)?',
            default=True,
        ):
            exit()
        npm_install(name, latest_version)
        return

    installed_version = dependencies[name]['version']
    print(f'npm - {name} found: v{installed_version}')
    npm_update_from_version(name, installed_version, latest_version)


def npm_update_from_version(
    name: str, installed_version: str, latest_version: str | None = None
):
    if not latest_version:
        latest_version = npm_get_latest_version(name)

    if installed_version == latest_version or not click.confirm(
        f'npm - Update {name} to v{latest_version}?', default=False
    ):
        return

    run(['sudo', 'npm', '-g', 'install', f'{name}@{latest_version}'], check=True)


def check_apt():
    packages = ['npm', 'dpkg']
    versions = apt_get_installed_versions(*packages)

    for name in packages:
        if version := versions.get(name):
            print(f'apt - {name} found: v{version}')
            continue
        if not click.confirm(
            f'apt - {name} not found! Install (required)?', default=True
        ):
            exit()
        run(['sudo', 'apt', 'install', name])


def check_npm():
    result = None

    try:
        result = run(
            ['npm', '--version'],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print('npm not found!')
        exit()

    version = result.stdout.strip()
    print(f'npm found: v{version}')

    npm_update_from_version('npm', version)
    npm_install('electron')
    npm_install('@electron/asar')


def main():
    check_apt()
    check_npm()

    request_url = 'https://discord.com/api/download?platform=linux&format=tar.gz'
    url = requests.head(request_url, allow_redirects=True).url

    match = re.fullmatch(r'.*/((\S+)-(\d+\.\d+\.\d+)\.tar\.gz)', url)
    if not match:
        print('Invalid response URL: {url}')
        exit()

    name = match[2]
    version = match[3]

    deb_package = 'discord-electron'
    installed_version = apt_get_installed_version(deb_package)

    default_install = installed_version != version

    if installed_version == version:
        print(f'* {deb_package} v{version} is already installed.')

    if not click.confirm(
        f'Build Debian package for {name} {version}?', default=default_install
    ):
        exit()

    root = Path(__file__).parent.absolute()
    os.chdir(root)

    archives = root / 'archives'
    archive = archives / match[1]

    if not archives.is_dir():
        archives.mkdir()

    must_download = not archive.is_file()

    if must_download:
        print('Downloading archive...')
        run(f'wget -c {url!r}', check=True, shell=True, cwd=archives)

    print('Decompressing archive...')
    run(['tar', '-xzf', archive], check=True)

    src = root / 'Discord'

    print('Patching sources...')

    package_name = 'discord'
    dest = Path('/usr/local').expanduser()
    _bin = Path('bin')
    binary = _bin / package_name
    lib = Path('lib') / package_name
    share = Path('share')
    pixmaps = share / 'pixmaps'

    file = src / 'discord.desktop'
    s = file.read_text()
    s = re.sub('(Exec=).*', fr'\1{dest / binary}', s)
    s = re.sub('(Path=).*', fr'\1{dest / _bin}', s)
    file.write_text(s)

    os.chdir(src)

    app_asar = Path('resources/app.asar')
    app = Path('resources/app')
    run(['asar', 'e', app_asar, app], check=True)
    app_asar.unlink()

    file = Path('resources/app/app_bootstrap/buildInfo.js')
    s = file.read_text()
    s = s.replace('process.resourcesPath', repr(str(dest / lib)))
    file.write_text(s)

    file = Path('resources/app/app_bootstrap/autoStart/linux.js')
    s = file.read_text()
    s = s.replace('exeDir,', f'{str(dest / pixmaps)!r},')
    file.write_text(s)

    run(['asar', 'p', app, app_asar], check=True)
    rmtree(app)

    os.chdir(root)

    build = root / 'build'
    deb = build / deb_package

    print('Creating installation files...')
    if build.exists():
        rmtree(build)
    build.mkdir()

    deb.mkdir()
    copytree('DEBIAN', deb / 'DEBIAN', dirs_exist_ok=True)
    control = deb / 'DEBIAN' / 'control'
    s = control.read_text()
    s = s.replace('__VERSION__', version)
    control.write_text(s)
    dst = deb / dest.relative_to('/')

    s = f'''#!/bin/bash
electron {str(dest / lib / 'app.asar')!r} "$@"
'''
    file = src / 'launcher.sh'
    file.write_text(s)
    binary = dst / binary
    run(['install', '-Dm', '755', file, binary], check=True)

    lib = dst / lib
    run(['install', '-d', lib], check=True)
    copytree(src / 'resources', lib, dirs_exist_ok=True)

    run(
        'install -d {bin,share/{pixmaps,applications}}',
        check=True,
        cwd=dst,
        executable='bash',
        shell=True,
    )

    pixmaps = dst / pixmaps
    copy(src / 'discord.png', pixmaps / f'{package_name}.png')

    applications = dst / share / 'applications'
    copy(
        src / 'discord.desktop',
        applications / f'{package_name}.desktop',
    )

    rmtree(src)

    if must_download and click.confirm('Delete archive?', default=True):
        archive.unlink()
        if not os.listdir(archives):
            archives.rmdir()

    print('Creating Debian package...')
    os.chdir(build)
    run(['dpkg-deb', '--build', deb_package], check=True)

    file = Path(f'{deb_package}.deb')
    if click.confirm(f'Install {file}?', default=False):
        run(['sudo', 'apt', 'install', file.absolute()])


if __name__ == '__main__':
    main()
