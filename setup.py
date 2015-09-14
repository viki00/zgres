import os
from setuptools import setup, find_packages

with open('debian/changelog') as f:
    _, version = f.read().splitlines()[0].split('(', 1)
    version, _ = version.split(')', 1)
    version = version.strip()

setup(name="zgres",
      version=version,
      packages=find_packages(),
      install_requires=['kazoo'],
      description="Database Connection and failiver manager for PostgreSQL",
      entry_points = {
          'console_scripts': [
              'zgres-apply = zgres.apply:apply_cli',
              'zgres-sync = zgres.sync:sync_cli',
              'zgres-deadman = zgres.deadman:deadman_cli',
          ],
          'zgres.conn': [
              'zgres-apply = zgres.apply:conn_info_plugin_factory',
          },
      include_package_data = True,
      zip_safe = True,
      )
