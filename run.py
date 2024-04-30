#!.venv/bin/python

import json
import os
import re
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path
from shutil import copy, copytree, rmtree
from subprocess import run

import click
import requests

_DEB_PACKAGE = 'discord-electron'
_INSTALLED_VERSION_PATTERN = re.compile(r'^(\S+)/\S+ (\S+) ')
_REQUEST_URL = 'https://discord.com/api/download?platform=linux&format=tar.gz'


@dataclass
class VersionInfo:
    url: str
    archive: str
    name: str
    version: str


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


def npm_update_from_version(name: str, installed_version: str, latest_version: str | None = None):
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
        if not click.confirm(f'apt - {name} not found! Install (required)?', default=True):
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


def get_version_info() -> VersionInfo:
    url = requests.head(_REQUEST_URL, allow_redirects=True).url

    match = re.fullmatch(r'.*/((\S+)-(\d+\.\d+\.\d+)\.tar\.gz)', url)
    if not match:
        print('Invalid response URL: {url}')
        exit(-1)

    return VersionInfo(url=url, archive=match[1], name=match[2], version=match[3])


def _do_needs_update():
    info = get_version_info()
    needs = apt_get_installed_version(_DEB_PACKAGE) != info.version
    print(info.url)
    print('Update needed:', needs)
    exit(1 if needs else 0)


def _do_install(full: bool):
    if full:
        check_apt()
        check_npm()

    version_info = get_version_info()
    version = version_info.version
    installed_version = apt_get_installed_version(_DEB_PACKAGE)
    already_installed = version == installed_version

    if already_installed:
        print(f'* {_DEB_PACKAGE} v{version} is already installed.')
    else:
        print(f'Installing {_DEB_PACKAGE} v{version}...')

    if full and not click.confirm(
        f'Build Debian package for {version_info.name} {version}?', default=not already_installed
    ):
        exit()

    root = Path(__file__).parent.absolute()
    os.chdir(root)

    archives = root / 'archives'
    archive = archives / version_info.archive

    if not archives.is_dir():
        archives.mkdir()

    must_download = not archive.is_file()

    if must_download:
        print('Downloading archive...')
        run(f'wget -c {version_info.url!r}', check=True, shell=True, cwd=archives)

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

    file = Path('resources/app/common/paths.js')
    s = file.read_text()
    s = re.sub(r'\s*(?:let )?resourcesPath = .*;', '', s)
    s = s.replace('return resourcesPath', f'return {str(dest / lib)!r}')
    file.write_text(s)

    file = Path('resources/app/app_bootstrap/autoStart/linux.js')
    s = file.read_text()
    s = s = re.sub('(Exec=).*', fr'\1{dest / binary}', s)
    s = s = re.sub('(Name=).*', fr'\1{package_name}', s)
    s = s = re.sub('(Icon=).*', fr'\1{package_name}', s)
    file.write_text(s)

    run(['asar', 'p', app, app_asar], check=True)
    rmtree(app)

    os.chdir(root)

    build = root / 'build'
    deb = build / _DEB_PACKAGE

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

if which update-discord; then
    update-discord needs-update
    if [[ "$?" == 1 ]]; then
        konsole -e 'update-discord --silent'
    fi
fi

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

    if full and must_download and click.confirm('Delete archive?', default=True):
        archive.unlink()
        if not os.listdir(archives):
            archives.rmdir()

    print('Creating Debian package...')
    os.chdir(build)
    run(['dpkg-deb', '--build', _DEB_PACKAGE], check=True)

    file = Path(f'{_DEB_PACKAGE}.deb')
    if not full or click.confirm(f'Install {file}?', default=True):
        run(['sudo', 'apt', 'install', '--reinstall', '-y', file.absolute()])

    if full:
        print('Finished! Press any key to exit.')
        input()


def main():
    parser = ArgumentParser(
        prog='update-discord',
        description='Updates Discord and associated system libraries',
    )
    parser.add_argument('--silent', action='store_true')
    parser.set_defaults(fn=None)

    parsers = parser.add_subparsers(title='action')

    sub_parser = parsers.add_parser(
        'needs-update',
        description='Checks if an update is needed',
    )
    sub_parser.set_defaults(fn=_do_needs_update)

    args = parser.parse_args()

    if args.fn:
        args.fn()
        exit()

    _do_install(not args.silent)


if __name__ == '__main__':
    main()
