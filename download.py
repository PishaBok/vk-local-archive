#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Download a VK archive. This command never starts the web interface."""

from vk_archive.download_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
