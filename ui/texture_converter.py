# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------------------------------------------
#   Reflex Package Tool — A tool for working with game archives for MX vs ATV Reflex in the .package format.
#   Copyright (C) 2026  Daniil Korochansky
#
#   This file is part of Reflex Package Tool.
#
#   Reflex Package Tool is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   Reflex Package Tool is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with Reflex Package Tool.  If not, see <https://www.gnu.org/licenses/>.
# -------------------------------------------------------------------------------------------------------------------

from __future__ import annotations

import threading
from pathlib import Path

import wx
import wx.propgrid
from PIL import Image

import core.reflex_texture as texture_backend


APP_NAME = "Texture Converter"


class TextureLoadProgress(wx.Dialog):
    """Small indeterminate progress dialog used while reading a texture."""

    def __init__(self, parent):
        super().__init__(
            parent,
            title="Opening texture",
            size=(360, 105),
            style=wx.DEFAULT_DIALOG_STYLE & ~wx.CLOSE_BOX,
        )

        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        self.label = wx.StaticText(
            panel,
            label="Opening...",
        )

        self.gauge = wx.Gauge(
            panel,
            range=100,
            style=wx.GA_HORIZONTAL,
        )

        root.Add(self.label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
        root.Add(self.gauge, 0, wx.EXPAND | wx.ALL, 12)

        panel.SetSizer(root)
        self.CentreOnParent()

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._pulse, self.timer)

    def start(self):
        self.timer.Start(80)

    def stop(self):
        if self.timer.IsRunning():
            self.timer.Stop()

    def _pulse(self, event):
        self.gauge.Pulse()


class TextureConverter(wx.Dialog):
    def __init__(self, parent=None):
        super().__init__(
            parent,
            id=wx.ID_ANY,
            title=APP_NAME,
            pos=wx.DefaultPosition,
            size=wx.Size(800, 600),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.SetSizeHints(wx.Size(600, 360), wx.DefaultSize)

        self.current_texture: Path | None = None
        self.current_info: dict | None = None
        self.current_preview = None

        self._preview_zoom = 1.0
        self._preview_fit_zoom = 1.0
        self._preview_min_zoom = 0.2
        self._preview_max_zoom = 3.0
        self._preview_offset_x = 0.0
        self._preview_offset_y = 0.0
        self._preview_dragging = False
        self._preview_drag_start = None
        self._preview_offset_start = None

        self._build_ui()
        self._bind_events()
        self._clear_texture_info()

        self.Centre(wx.BOTH)

    def on_exit(self, event):
        self.EndModal(wx.ID_OK)

    def _build_ui(self):
        main = wx.BoxSizer(wx.VERTICAL)

        self.m_splitter = wx.SplitterWindow(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.SP_3D | wx.SP_LIVE_UPDATE,
        )
        self.m_splitter.SetMinimumPaneSize(260)

        # Left
        self.m_left_panel_splitter = wx.Panel(
            self.m_splitter,
            wx.ID_ANY,
        )

        left_sizer = wx.BoxSizer(wx.VERTICAL)

        self.m_toolBar = wx.ToolBar(
            self.m_left_panel_splitter,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.TB_FLAT | wx.TB_HORIZONTAL | wx.TB_NODIVIDER | wx.TB_TEXT,
        )
        self.m_toolBar.SetToolBitmapSize(wx.Size(24, 24))

        self.m_tool_OpenTexture = self.m_toolBar.AddTool(
            wx.ID_ANY,
            "Open...",
            wx.ArtProvider.GetBitmap(
                wx.ART_FILE_OPEN,
                wx.ART_TOOLBAR,
            ),
            wx.NullBitmap,
            wx.ITEM_NORMAL,
            "Open a .texture file",
            wx.EmptyString,
        )

        self.m_toolBar.AddSeparator()

        self.m_choice_convertType = wx.Choice(
            self.m_toolBar,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            ["To .dds", "From .dds"],
            0,
        )
        self.m_choice_convertType.SetSelection(0)
        self.m_choice_convertType.SetToolTip(
            "Type of conversion\n\n"
            '- "To .dds": Convert the opened .texture to .dds\n'
            '- "From .dds": Select the modified .dds and convert it to .texture'
        )
        self.m_toolBar.AddControl(self.m_choice_convertType)

        self.m_toolConvert = self.m_toolBar.AddTool(
            wx.ID_ANY,
            "Convert",
            wx.ArtProvider.GetBitmap(
                wx.ART_FILE_SAVE,
                wx.ART_TOOLBAR,
            ),
            wx.NullBitmap,
            wx.ITEM_NORMAL,
            'Converting ".texture" to ".dds" or ".dds" to ".texture"',
            wx.EmptyString,
        )

        self.m_toolBar.AddSeparator()

        self.m_toolExit = self.m_toolBar.AddTool(
            wx.ID_ANY,
            "Close",
            wx.ArtProvider.GetBitmap(
                wx.ART_QUIT,
                wx.ART_TOOLBAR,
            ),
            wx.NullBitmap,
            wx.ITEM_NORMAL,
            'Close “Texture Converter”',
            wx.EmptyString,
        )

        self.m_toolBar.Realize()
        left_sizer.Add(self.m_toolBar, 0, wx.EXPAND)

        self.m_propertyGridManager = wx.propgrid.PropertyGridManager(
            self.m_left_panel_splitter,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.propgrid.PGMAN_DEFAULT_STYLE
            | wx.propgrid.PG_DESCRIPTION
            | wx.propgrid.PG_HIDE_MARGIN
            | wx.propgrid.PG_LIMITED_EDITING
            | wx.propgrid.PG_TOOLTIPS,
        )
        
        self.m_propertyGridPage = self.m_propertyGridManager.AddPage(
            "Texture"
        )

        self.m_propertyGridItem_TextureName = self.m_propertyGridPage.Append(
            wx.propgrid.StringProperty("Name")
        )
        self.m_propertyGridItem_TextureType = self.m_propertyGridPage.Append(
            wx.propgrid.StringProperty("Type")
        )
        self.m_propertyGridItem_TextureFormat = self.m_propertyGridPage.Append(
            wx.propgrid.StringProperty("Format")
        )
        self.m_propertyGridItem_TextureWidth = self.m_propertyGridPage.Append(
            wx.propgrid.StringProperty("Width")
        )
        self.m_propertyGridItem_TextureHeight = self.m_propertyGridPage.Append(
            wx.propgrid.StringProperty("Height")
        )
        self.m_propertyGridItem_TextureMipmaps = self.m_propertyGridPage.Append(
            wx.propgrid.StringProperty("Mipmaps")
        )

        # PropertyGrid is informational only.
        for prop in (
            self.m_propertyGridItem_TextureName,
            self.m_propertyGridItem_TextureType,
            self.m_propertyGridItem_TextureFormat,
            self.m_propertyGridItem_TextureWidth,
            self.m_propertyGridItem_TextureHeight,
            self.m_propertyGridItem_TextureMipmaps,
        ):
            prop.Enable(False)

        left_sizer.Add(
            self.m_propertyGridManager,
            1,
            wx.ALL | wx.EXPAND,
            5,
        )
        
        self.m_left_panel_splitter.SetSizer(left_sizer)

        # Right / preview
        self.m_right_panel_splitter = wx.Panel(
            self.m_splitter,
            wx.ID_ANY,
        )

        preview_sizer = wx.BoxSizer(wx.VERTICAL)

        self.m_panel_Preview = wx.Panel(
            self.m_right_panel_splitter,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.BORDER_SIMPLE | wx.TAB_TRAVERSAL,
        )
        self.m_panel_Preview.SetBackgroundStyle(wx.BG_STYLE_PAINT)

        preview_sizer.Add(
            self.m_panel_Preview,
            1,
            wx.EXPAND | wx.ALL,
            5,
        )

        self.m_right_panel_splitter.SetSizer(preview_sizer)

        self.m_splitter.SplitVertically(
            self.m_left_panel_splitter,
            self.m_right_panel_splitter,
            260,
        )

        main.Add(self.m_splitter, 1, wx.EXPAND)

        self.SetSizer(main)
        
        self.Layout()
        self.m_propertyGridManager.SetDescBoxHeight(350)
        self.m_propertyGridManager.SetSplitterPosition(70)
        self.m_propertyGridManager.Refresh()

    def _bind_events(self):
        self.Bind(
            wx.EVT_TOOL,
            self.on_open_texture,
            id=self.m_tool_OpenTexture.GetId(),
        )
        self.Bind(
            wx.EVT_TOOL,
            self.on_convert,
            id=self.m_toolConvert.GetId(),
        )

        self.Bind(
            wx.EVT_TOOL,
            self.on_exit,
            id=self.m_toolExit.GetId(),
        )

        self.m_propertyGridManager.Bind(
            wx.propgrid.EVT_PG_SELECTED,
            self.on_property_selected,
        )

        self.m_panel_Preview.Bind(
            wx.EVT_PAINT,
            self.on_preview_paint,
        )
        self.m_panel_Preview.Bind(
            wx.EVT_SIZE,
            self.on_preview_size,
        )
        self.m_panel_Preview.Bind(
            wx.EVT_MOUSEWHEEL,
            self.on_preview_wheel,
        )
        self.m_panel_Preview.Bind(wx.EVT_LEFT_DOWN, self.on_preview_left_down)
        self.m_panel_Preview.Bind(wx.EVT_LEFT_UP, self.on_preview_left_up)
        self.m_panel_Preview.Bind(wx.EVT_MOTION, self.on_preview_motion)
        self.m_panel_Preview.Bind(wx.EVT_LEAVE_WINDOW, self.on_preview_leave)


    # ------------------------------------------------------------------
    # Property information
    # ------------------------------------------------------------------

    @staticmethod
    def _format_help(fmt: str) -> str:
        common = {
            "DXT1": (
                "Instructions for editing a DXT1 texture:\n\n"
                "Cube Map not supported!\n\n"
                "1. Convert .texture to .dds\n"
                "2. Edit (Recommended on Paint.Net with the\nDdsFileTypePlus plugin by Nicholas Hayes)\n"
                "3. Be sure to save the .dds texture with the following settings:\n\n"
                "Format: BC1 (Linear, DXT1)\n"
                "Error diffusion dithering: Not checked\n"
                "Error Metric: Perceptual\n"
                "Generate Mip Maps: Checked\n"
                "Compression and resampling algorithm: Adaptive (Sharp)\n\n"
                "4. Change the conversion type to 'From .dds,' click 'Convert', select\n"
                "your .dds file, and save the .texture file to the desired location."
            ),
            "DXT5": (
                "Instructions for editing a DXT5 texture:\n\n"
                "1. Convert .texture to .dds\n"
                "2. Edit (Recommended on Paint.Net with the\nDdsFileTypePlus plugin by Nicholas Hayes)\n"
                "3. Be sure to save the .dds texture with the following settings:\n\n"
                "Format: BC3 (Linear, DXT5)\n"
                "Error diffusion dithering: Not checked\n"
                "Error Metric: Perceptual\n"
                "Generate Mip Maps: Checked\n"
                "Compression and resampling algorithm: Adaptive (Sharp)\n\n"
                "4. Change the conversion type to 'From .dds,' click 'Convert', select\n"
                "your .dds file, and save the .texture file to the desired location."
            ),
            "0x70": (
                "Instructions for editing a 0x70 (R16G16_FLOAT) texture:\n\n"
                "1. Convert .texture to .dds\n"
                "2. Open the .dds texture in GIMP (or any other editor\nthat supports R16G16 Float)\n"
                "3. Edit\n"
                "4. Export as a .tif image\n"
                "5. Download TexConv from Microsoft.\n"
                "6. Convert .tif to the desired .dds format using the\nfollowing command in the command line:\n\n"
                "texconv.exe -f R16G16_FLOAT -dx10 your_texture.tif\n\n"
                "7. Change the conversion type to 'From .dds,' click 'Convert', select\n"
                "your .dds file, and save the .texture file to the desired location."
            ),
            "A8R8G8B8": (
                "Instructions for editing a A8R8G8B8 texture:\n\n"
                "1. Convert .texture to .dds\n"
                "2. Edit (Recommended on Paint.Net with the\nDdsFileTypePlus plugin by Nicholas Hayes)\n"
                "3. Be sure to save the .dds texture with the following settings:\n\n"
                "Format: B8G8R8A8 (Linear, A8R8G8B8)\n"
                "Error diffusion dithering: -\n"
                "Error Metric: -\n"
                "Generate Mip Maps: Checked\n"
                "Compression and resampling algorithm: Adaptive (Sharp)\n\n"
                "4. Change the conversion type to 'From .dds,' click 'Convert', select\n"
                "your .dds file, and save the .texture file to the desired location."
            ),
            "R32F": (
                "Instructions for editing a R32F texture:\n\n"
                "1. Convert .texture to .dds\n"
                "2. Edit (Recommended on Paint.Net with the\nDdsFileTypePlus plugin by Nicholas Hayes)\n"
                "3. Be sure to save the .dds texture with the following settings:\n\n"
                "Format: R32 (Linear, Float)\n"
                "Generate Mip Maps: Checked\n"
                "Compression and resampling algorithm: Adaptive (Sharp)\n\n"
                "4. Change the conversion type to 'From .dds,' click 'Convert', select\n"
                "your .dds file, and save the .texture file to the desired location."
            ),
            "A16B16G16R16F": (
                "Instructions for editing a A16B16G16R16F texture:\n\n"
                "1. Convert .texture to .dds\n"
                "2. Install Adobe Photoshop\n"
                "3. Install the NVIDIA Texture Tools Exporter plug-in (or a similar\none from Intel) in Adobe Photoshop.\n"
                "4. Open the .dds texture in Adobe Photoshop\n"
                "5. Edit\n"
                "6. Export the .dds file using the NVIDIA Texture Tools\nExporter with the following settings:\n\n"
                "Format: A16B16G16R16F (16-bit half-float)\n"
                "Texture: 2D\n"
                "Generate MIP maps: Checked\n"
                "Error Metric: Perceptual\n"
                "7. Change the conversion type to 'From .dds,' click 'Convert', select\n"
                "your .dds file, and save the .texture file to the desired location."
            ),
        }
        return common.get(
            fmt,
            f"Format {fmt}.\n\n"
            "This format is recognized by the Texture Converter backend. "
            "Keep the original format when saving the DDS.",
        )

    @staticmethod
    def _type_help(info: dict) -> str:
        if info.get("faces", 1) == 6:
            return (
                "Cube Map.\n\n"
                "Not allowed:\n"
                "• Do not increase the CubeMap resolution in this workflow.\n"
                "• Do not turn the cross into an ordinary 2D texture when importing."
            )

        return (
            "2D texture.\n\n"
            "The texture can be edited as an ordinary DDS while preserving "
            "its original format."
        )

    def _set_help(self, prop, text: str):
        self.m_propertyGridPage.SetPropertyHelpString(prop, text)

    def on_property_selected(self, event):
        prop = event.GetProperty()
        if prop is None:
            event.Skip()
            return

        name = prop.GetName()

        if name == "Format" and self.current_info:
            self._set_help(
                prop,
                self._format_help(self.current_info["format"]),
            )

        elif name == "Type" and self.current_info:
            self._set_help(
                prop,
                self._type_help(self.current_info),
            )

        elif name == "Name":
            self._set_help(
                prop,
                "Name of the opened .texture resource.",
            )

        elif name == "Width":
            self._set_help(
                prop,
                "Original texture width in pixels.",
            )

        elif name == "Height":
            self._set_help(
                prop,
                "Original texture height in pixels.",
            )

        elif name == "Mipmaps":
            self._set_help(
                prop,
                "Number of mip levels stored in the Reflex texture.",
            )

        event.Skip()

    # ------------------------------------------------------------------
    # Texture opening
    # ------------------------------------------------------------------

    def on_open_texture(self, event=None):
        wildcard = (
            "MX vs ATV Reflex Texture (*.texture)|*.texture|"
            "All files (*.*)|*.*"
        )

        with wx.FileDialog(
            self,
            "Open .texture",
            wildcard=wildcard,
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return

            path = Path(dialog.GetPath())

        self._load_texture(path)

    def _load_texture(self, path: Path):
        progress = TextureLoadProgress(self)
        progress.start()
        progress.Show()

        def worker():
            try:
                info = texture_backend.inspect_texture(path)
                preview = texture_backend.get_texture_preview(path)
                wx.CallAfter(self._finish_texture_load, path, info, preview, None)
            except Exception as exc:
                wx.CallAfter(self._finish_texture_load, path, None, None, exc)

        threading.Thread(
            target=worker,
            name="TexturePreviewLoader",
            daemon=True,
        ).start()

        # Keep a reference until CallAfter finishes.
        self._load_progress = progress

    def _finish_texture_load(self, path, info, preview, error):
        progress = getattr(self, "_load_progress", None)
        if progress is not None:
            progress.stop()
            progress.Destroy()
            self._load_progress = None

        if error is not None:
            wx.MessageBox(
                str(error),
                "Open texture failed",
                wx.OK | wx.ICON_ERROR,
            )
            return

        self.current_texture = Path(path)
        self.current_info = info
        self.current_preview = preview

        self._fill_properties(info, self.current_texture.name)

        self._preview_zoom = 1.0
        self._preview_fit_zoom = 1.0
        self._preview_min_zoom = 0.2
        self._preview_max_zoom = 3.0
        self._preview_offset_x = 0.0
        self._preview_offset_y = 0.0
        self._preview_dragging = False
        self._preview_drag_start = None
        self._preview_offset_start = None

        self.m_panel_Preview.Refresh()
        self.m_panel_Preview.Update()

    def _fill_properties(self, info: dict, name: str):
        values = {
            self.m_propertyGridItem_TextureName: name,
            self.m_propertyGridItem_TextureType: info["type"],
            self.m_propertyGridItem_TextureFormat: info["format"],
            self.m_propertyGridItem_TextureWidth: f'{info["width"]} px',
            self.m_propertyGridItem_TextureHeight: f'{info["height"]} px',
            self.m_propertyGridItem_TextureMipmaps: str(info["mipmaps"]),
        }

        for prop, value in values.items():
            prop.SetValue(str(value))
            prop.Enable(False)

        self._set_help(
            self.m_propertyGridItem_TextureName,
            "Name of the opened .texture resource.",
        )
        self._set_help(
            self.m_propertyGridItem_TextureType,
            self._type_help(info),
        )
        self._set_help(
            self.m_propertyGridItem_TextureFormat,
            self._format_help(info["format"]),
        )
        self._set_help(
            self.m_propertyGridItem_TextureWidth,
            "Original texture width in pixels.",
        )
        self._set_help(
            self.m_propertyGridItem_TextureHeight,
            "Original texture height in pixels.",
        )
        self._set_help(
            self.m_propertyGridItem_TextureMipmaps,
            "Number of mip levels stored in the Reflex texture.",
        )

    def _clear_texture_info(self):
        for prop in (
            self.m_propertyGridItem_TextureName,
            self.m_propertyGridItem_TextureType,
            self.m_propertyGridItem_TextureFormat,
            self.m_propertyGridItem_TextureWidth,
            self.m_propertyGridItem_TextureHeight,
            self.m_propertyGridItem_TextureMipmaps,
        ):
            prop.SetValue("")
            prop.Enable(False)

        self.m_panel_Preview.Refresh()

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def on_convert(self, event=None):
        if self.current_texture is None or self.current_info is None:
            wx.MessageBox(
                "Open a .texture file first.",
                "Convert",
                wx.OK | wx.ICON_INFORMATION,
            )
            return

        if self.m_choice_convertType.GetSelection() == 0:
            self._convert_to_dds()
        else:
            self._convert_from_dds()

    def _convert_to_dds(self):
        default_name = self.current_texture.stem + ".dds"

        with wx.FileDialog(
            self,
            "Save DDS",
            defaultDir=str(self.current_texture.parent),
            defaultFile=default_name,
            wildcard="DirectDraw Surface (*.dds)|*.dds|All files (*.*)|*.*",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return

            output = Path(dialog.GetPath())

        try:
            texture_backend.decode_texture_to_dds(
                self.current_texture,
                output,
            )
        except Exception as exc:
            wx.MessageBox(
                str(exc),
                "DDS conversion failed",
                wx.OK | wx.ICON_ERROR,
            )
            return

        wx.MessageBox(
            f"DDS successfully created:\n\n{output}",
            "Conversion complete",
            wx.OK | wx.ICON_INFORMATION,
        )

    def _convert_from_dds(self):
        with wx.FileDialog(
            self,
            "Open modified DDS",
            wildcard="DirectDraw Surface (*.dds)|*.dds|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return

            dds_path = Path(dialog.GetPath())

        default_name = self.current_texture.name

        with wx.FileDialog(
            self,
            "Save .texture",
            defaultDir=str(self.current_texture.parent),
            defaultFile=default_name,
            wildcard="MX vs ATV Reflex Texture (*.texture)|*.texture|All files (*.*)|*.*",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return

            output = Path(dialog.GetPath())

        try:
            texture_backend.encode_dds_to_texture(
                self.current_texture,
                dds_path,
                output,
            )
        except Exception as exc:
            wx.MessageBox(
                str(exc),
                "Texture conversion failed",
                wx.OK | wx.ICON_ERROR,
            )
            return

        wx.MessageBox(
            f"Texture successfully created:\n\n{output}",
            "Conversion complete",
            wx.OK | wx.ICON_INFORMATION,
        )

    # ------------------------------------------------------------------
    # Preview / checkerboard / zoom
    # ------------------------------------------------------------------

    def on_preview_size(self, event):
        if self.current_preview is not None:
            # Keep the current zoom on resize, but re-clamp the pan position.
            self._update_fit_zoom()
            self._clamp_preview_offset()
        self.m_panel_Preview.Refresh(False)
        event.Skip()

    def _fit_preview_to_window(self):
        if self.current_preview is None:
            return

        pw, ph = self.m_panel_Preview.GetClientSize()
        iw, ih = self.current_preview.size

        if pw <= 0 or ph <= 0 or iw <= 0 or ih <= 0:
            return

        margin = 12
        zx = max(0.01, (pw - margin * 2) / iw)
        zy = max(0.01, (ph - margin * 2) / ih)

        self._preview_fit_zoom = min(zx, zy)
        self._preview_min_zoom = max(0.05, self._preview_fit_zoom * 0.25)
        self._preview_zoom = self._preview_fit_zoom
        self._preview_offset_x = 0.0
        self._preview_offset_y = 0.0
        self._clamp_preview_offset()
        self.m_panel_Preview.Refresh(False)

    def _update_fit_zoom(self):
        if self.current_preview is None:
            return

        pw, ph = self.m_panel_Preview.GetClientSize()
        iw, ih = self.current_preview.size

        if pw <= 0 or ph <= 0 or iw <= 0 or ih <= 0:
            return

        margin = 12
        zx = max(0.01, (pw - margin * 2) / iw)
        zy = max(0.01, (ph - margin * 2) / ih)

        self._preview_fit_zoom = min(zx, zy)
        self._preview_min_zoom = max(0.05, self._preview_fit_zoom * 0.25)

    def _image_draw_rect(self):
        if self.current_preview is None:
            return None

        pw, ph = self.m_panel_Preview.GetClientSize()
        iw, ih = self.current_preview.size

        draw_w = max(1, int(round(iw * self._preview_zoom)))
        draw_h = max(1, int(round(ih * self._preview_zoom)))

        x = (pw - draw_w) / 2.0 + self._preview_offset_x
        y = (ph - draw_h) / 2.0 + self._preview_offset_y

        return x, y, draw_w, draw_h

    def _clamp_preview_offset(self):
        if self.current_preview is None:
            self._preview_offset_x = 0.0
            self._preview_offset_y = 0.0
            return

        pw, ph = self.m_panel_Preview.GetClientSize()
        rect = self._image_draw_rect()
        if rect is None:
            return

        x, y, iw, ih = rect

        # If the image is smaller than the panel, keep it centered.
        if iw <= pw:
            self._preview_offset_x = 0.0
        else:
            # At least a thin part of the image always remains inside the panel.
            max_pan_x = iw / 2.0 + pw / 2.0 - 8.0
            self._preview_offset_x = max(
                -max_pan_x,
                min(max_pan_x, self._preview_offset_x),
            )

        if ih <= ph:
            self._preview_offset_y = 0.0
        else:
            max_pan_y = ih / 2.0 + ph / 2.0 - 8.0
            self._preview_offset_y = max(
                -max_pan_y,
                min(max_pan_y, self._preview_offset_y),
            )

    @staticmethod
    def _draw_checkerboard(dc, rect, cell=12):
        x0, y0, w, h = rect

        c1 = wx.Colour(232, 232, 232)
        c2 = wx.Colour(205, 205, 205)

        for y in range(y0, y0 + h, cell):
            for x in range(x0, x0 + w, cell):
                odd = ((x - x0) // cell + (y - y0) // cell) & 1
                dc.SetBrush(wx.Brush(c1 if odd == 0 else c2))
                dc.SetPen(wx.TRANSPARENT_PEN)
                dc.DrawRectangle(
                    x,
                    y,
                    min(cell, x0 + w - x),
                    min(cell, y0 + h - y),
                )

    def on_preview_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self.m_panel_Preview)

        w, h = self.m_panel_Preview.GetClientSize()
        self._draw_checkerboard(dc, (0, 0, w, h))

        if self.current_preview is None:
            return

        image = self.current_preview
        zoom = self._preview_zoom

        draw_w = max(1, int(round(image.width * zoom)))
        draw_h = max(1, int(round(image.height * zoom)))

        scaled = image.resize(
            (draw_w, draw_h),
            resample=Image.LANCZOS,
        )

        # RGB and alpha must be supplied to wx.Image separately.
        rgb = scaled.convert("RGB")
        alpha = scaled.getchannel("A")

        wx_image = wx.Image(draw_w, draw_h)
        wx_image.SetData(rgb.tobytes())
        wx_image.SetAlpha(alpha.tobytes())

        bitmap = wx.Bitmap(wx_image)

        x, y, _, _ = self._image_draw_rect()
        dc.DrawBitmap(bitmap, int(round(x)), int(round(y)), True)

    def on_preview_wheel(self, event):
        if self.current_preview is None:
            return

        rotation = event.GetWheelRotation()
        if rotation == 0:
            return

        # Zoom toward the mouse cursor so the point under the cursor stays fixed.
        mouse_x, mouse_y = event.GetPosition()
        pw, ph = self.m_panel_Preview.GetClientSize()

        old_zoom = self._preview_zoom
        factor = 1.15 if rotation > 0 else (1.0 / 1.15)
        new_zoom = old_zoom * factor

        new_zoom = max(
            self._preview_min_zoom,
            min(self._preview_max_zoom, new_zoom),
        )

        if abs(new_zoom - old_zoom) < 1e-9:
            return

        # Coordinates of the image point currently under the mouse.
        old_left = (pw - self.current_preview.width * old_zoom) / 2.0 + self._preview_offset_x
        old_top = (ph - self.current_preview.height * old_zoom) / 2.0 + self._preview_offset_y

        image_x = (mouse_x - old_left) / old_zoom
        image_y = (mouse_y - old_top) / old_zoom

        new_left_without_pan = (pw - self.current_preview.width * new_zoom) / 2.0
        new_top_without_pan = (ph - self.current_preview.height * new_zoom) / 2.0

        self._preview_zoom = new_zoom
        self._preview_offset_x = (
            mouse_x - new_left_without_pan - image_x * new_zoom
        )
        self._preview_offset_y = (
            mouse_y - new_top_without_pan - image_y * new_zoom
        )

        self._clamp_preview_offset()
        self.m_panel_Preview.Refresh(False)

    def on_preview_left_down(self, event):
        if self.current_preview is None:
            event.Skip()
            return

        self._preview_dragging = True
        self._preview_drag_start = event.GetPosition()
        self._preview_offset_start = (
            self._preview_offset_x,
            self._preview_offset_y,
        )

        self.m_panel_Preview.CaptureMouse()
        self.m_panel_Preview.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        event.Skip()

    def on_preview_left_up(self, event):
        if self._preview_dragging:
            self._preview_dragging = False

            if self.m_panel_Preview.HasCapture():
                self.m_panel_Preview.ReleaseMouse()

            self._clamp_preview_offset()
            self.m_panel_Preview.SetCursor(wx.NullCursor)
            self.m_panel_Preview.Refresh(False)

        event.Skip()

    def on_preview_motion(self, event):
        if not self._preview_dragging or self._preview_drag_start is None:
            event.Skip()
            return

        pos = event.GetPosition()
        start_x, start_y = self._preview_drag_start
        start_ox, start_oy = self._preview_offset_start

        self._preview_offset_x = start_ox + (pos.x - start_x)
        self._preview_offset_y = start_oy + (pos.y - start_y)

        self._clamp_preview_offset()
        self.m_panel_Preview.Refresh(False)
        event.Skip()

    def on_preview_leave(self, event):
        # Do not cancel an active drag; the panel has mouse capture.
        event.Skip()

    # ------------------------------------------------------------------

    def Destroy(self):
        progress = getattr(self, "_load_progress", None)
        if progress is not None:
            progress.stop()
            progress.Destroy()
            self._load_progress = None

        return super().Destroy()

