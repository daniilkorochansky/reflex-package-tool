[![Python](https://img.shields.io/badge/Python-3.x-3776AB)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6)](https://github.com/daniilkorochansky/reflex-package-tool)
[![Build](https://github.com/daniilkorochansky/reflex-package-tool/actions/workflows/build.yml/badge.svg)](https://github.com/daniilkorochansky/reflex-package-tool/actions/workflows/build.yml)
[![Latest Release](https://img.shields.io/github/v/release/daniilkorochansky/reflex-package-tool?display_name=tag)](https://github.com/daniilkorochansky/reflex-package-tool/releases)
[![License](https://img.shields.io/github/license/daniilkorochansky/reflex-package-tool)](https://github.com/daniilkorochansky/reflex-package-tool/blob/main/LICENSE)

# Reflex Package Tool
<img width="766" height="474" alt="image" src="https://github.com/user-attachments/assets/68ad4f8e-e078-46a7-81f3-996c815de551" />

A tool for viewing, exporting, and importing MX vs ATV Reflex game resources from .package game archives.

The tool uses [XMem Helper](https://github.com/daniilkorochansky/xmem-helper), which acts as a bridge between the 32-bit `mszip.dll` library and the 64-bit operating system.

## Features
+ Open `.package` files.
+ Automatic `.database` detection.
+ Extract resources from packages.
+ Replace resources inside packages.
+ Package optimization.
+ Automatic `.database` updating.
+ Append-only resource replacement.

## Table Of Contents
- [Installation](#installation)
- [Usage](#usage)
- [Package Optimization](#package-optimization)
- [Other Tools](#other-tools)
  - [Reflex BXML Editor](#other-tools)
  - [Reflex Font Package Viewer](#other-tools)

## Installation
1. Download the latest release.
2. Unzip the archive containing `Reflex Package Tool.exe`, `mszip.dll`, `xmem_helper.exe`, and `xmem_compress_helper.exe` into any folder.
3. Run `Reflex Package Tool.exe`.

## Usage
1. Open the `.package` archive.
2. Export the necessary resources.
3. Replace the previously exported resource.
4. Save the new `.package` and `.database` files to the desired folder, or replace them directly in the `Database` folder within the MX vs ATV Reflex game files.

## Package Optimization
Optimizing the .package file by repackaging the used data blocks into a new .package archive, thereby eliminating unused data blocks. As a result, the .package archive contains only the data that is actually used, which also reduces the size of the archive itself.

## Other Tools
+ [Reflex BXML Editor](https://github.com/daniilkorochansky/reflex-bxml-editor): It allows you to edit `.bxml`, `.database`, `.level` and `savegame.bxml` files and rebuild them.
+ [Reflex Font Package Viewer](https://github.com/daniilkorochansky/reflex-font-package-viewer): A tool for viewing and replacing character resources in the `data.fpack` file for the game MX vs ATV Reflex
