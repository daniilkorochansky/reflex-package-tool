[![Python](https://img.shields.io/badge/Python-3.x-3776AB)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6)](https://github.com/daniilkorochansky/reflex-package-tool)
[![Build](https://github.com/daniilkorochansky/reflex-package-tool/actions/workflows/build.yml/badge.svg)](https://github.com/daniilkorochansky/reflex-package-tool/actions/workflows/build.yml)
[![Latest Release](https://img.shields.io/github/v/release/daniilkorochansky/reflex-package-tool?display_name=tag)](https://github.com/daniilkorochansky/reflex-package-tool/releases)
[![License](https://img.shields.io/github/license/daniilkorochansky/reflex-package-tool)](https://github.com/daniilkorochansky/reflex-package-tool/blob/main/LICENSE)

# Reflex Package Tool
<img width="766" height="433" alt="image" src="https://github.com/user-attachments/assets/66d9b494-dc28-41f5-a7ef-c9903c603f9a" />


A tool for viewing, exporting, and replacing MX vs ATV Reflex game resources from .package game archives.

The tool uses [XMem Helper](https://github.com/daniilkorochansky/xmem-helper), which acts as a bridge between the 32-bit `mszip.dll` library and the 64-bit operating system.

## Features
+ Open `.package` files.
+ Automatic `.database` detection.
+ Extract resources from packages.
+ Replace resources inside packages.
+ Texture Converter.
+ Package optimization.
+ Automatic `.database` updating.
+ Append-only resource replacement.

## Table Of Contents
- [Installation](#installation)
- [Usage](#usage)
- [Package Optimization](#package-optimization)
- [Resources](#resources)
  - [Editable Resources](#editable-resources)
    - [Not supported](#not-supported)
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

## Resources
### Editable Resources
+ **.texture** - Textures for jersey suits, graphic kits, track objects, and so on. A built-in texture converter for converting between `.texture` and `.dds` formats.
+ **.bxml** - Including the `.material`, `.uicmpnt`, and `.adv` resources. These can be edited in the [Reflex Package Tool](https://github.com/daniilkorochansky/reflex-bxml-editor).
+ **.bink** - Bink video file in the .bik format. These are the videos that are shown on large stadium screens and elsewhere. Using Rad Video Tools, you can convert .mp4 files to the .bik format.
+ **.script** - Lua code for the game’s UI logic.
+ **.localiz** - Strings and localization keys. Edited using the built‑in Localization Editor.

#### Not supported
+ **.model** - This may be a configuration that describes which resources the 3D model uses and its parameters?
+ **.surface** - Equivalent to `.mesh`
+ **.sound** - It may describe which audio file, where, and when to play it?
+ **.anim** - Animation file
+ **.shader** - Shaders responsible for visual effects.
+ **.tree** - ?
+ **.forest** - ?
+ **.water** - ?
+ **.icongeom** - ?
+ **.tdf** - ?
+ **.texatlas** - ?
+ **.cell** - ?

## Other Tools
+ [Reflex BXML Editor](https://github.com/daniilkorochansky/reflex-bxml-editor): It allows you to edit `.bxml`, `.database`, `.level` and `savegame.bxml` files and rebuild them.
+ [Reflex Font Package Viewer](https://github.com/daniilkorochansky/reflex-font-package-viewer): A tool for viewing and replacing character resources in the `data.fpack` file for the game MX vs ATV Reflex
