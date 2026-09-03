

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import wx
import wx.dataview as dv

from core.reflex_localiz import (
    LocalizFile,
    LocalizError,
    decode_file,
    encode_file,
    make_localiz,
)


APP_TITLE = "Localization Editor"
FILTER_PLACEHOLDER = "Search keys or values..."


class LocalizationEditor(wx.Dialog):

    def __init__(
        self,
        parent: wx.Window | None = None,
        filepath: str | Path | None = None,
        *,
        title: str = APP_TITLE,
    ):
        super().__init__(
            parent,
            title=title,
            size=(1050, 500),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.SetSizeHints(wx.Size(500, 300), wx.DefaultSize)

        self.model: Optional[LocalizFile] = None
        self.source_path: Optional[Path] = None
        self.output_path: Optional[Path] = None
        self.modified = False
        self._visible_indices: list[int] = []
        self._updating_view = False

        self._build_ui()
        self._bind_events()

        self.CentreOnParent()

        if filepath:
            wx.CallAfter(self.open_file, Path(filepath))

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = wx.BoxSizer(wx.VERTICAL)
        self.root = root

        # Native toolbar
        self.toolbar = wx.ToolBar(
            self,
            style=wx.TB_HORIZONTAL | wx.TB_FLAT | wx.TB_TEXT,
        )
        self.toolbar.SetToolBitmapSize(wx.Size(20, 20))

        self.open_tool = self.toolbar.AddTool(
            wx.ID_OPEN,
            "Open...",
            wx.ArtProvider.GetBitmap(wx.ART_FILE_OPEN, wx.ART_TOOLBAR, wx.Size(20, 20)),
            shortHelp="Open localization resource",
        )

        self.save_as_tool = self.toolbar.AddTool(
            wx.ID_SAVEAS,
            "Build...",
            wx.ArtProvider.GetBitmap(wx.ART_FILE_SAVE_AS, wx.ART_TOOLBAR, wx.Size(20, 20)),
            shortHelp="Build a new localization resource",
        )

        self.toolbar.AddSeparator()

        self.close_tool = self.toolbar.AddTool(
            wx.ID_CLOSE,
            "Close",
            wx.ArtProvider.GetBitmap(wx.ART_QUIT, wx.ART_TOOLBAR, wx.Size(20, 20)),
            shortHelp="Close editor",
        )

        self.toolbar.Realize()
        root.Add(self.toolbar, 0, wx.EXPAND)

        # Search row
        search_sizer = wx.BoxSizer(wx.HORIZONTAL)
        search_sizer.Add(
            wx.StaticText(self, label="Find:"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            6,
        )
        self.search_ctrl = wx.SearchCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.search_ctrl.SetDescriptiveText(FILTER_PLACEHOLDER)
        self.search_ctrl.SetMinSize((320, -1))
        search_sizer.Add(self.search_ctrl, 1, wx.ALIGN_CENTER_VERTICAL)
        root.Add(search_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 10)

        # Main table
        self.view = dv.DataViewListCtrl(
            self,
            style=(
                dv.DV_ROW_LINES
                | dv.DV_VERT_RULES
                | dv.DV_HORIZ_RULES
                | dv.DV_MULTIPLE
            ),
        )

        self.key_column = self.view.AppendTextColumn(
            "Key",
            dv.DATAVIEW_CELL_INERT,
            350,
            flags=dv.DATAVIEW_COL_RESIZABLE | dv.DATAVIEW_COL_SORTABLE,
        )
        # Values are edited in a dedicated multiline dialog. This avoids
        # wxPython's single-line DataView editor truncating or destroying
        # embedded newlines.
        self.value_column = self.view.AppendTextColumn(
            "Value",
            dv.DATAVIEW_CELL_INERT,
            600,
            flags=dv.DATAVIEW_COL_RESIZABLE | dv.DATAVIEW_COL_SORTABLE,
        )

        root.Add(self.view, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # Bottom information line
        self.info_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.file_label = wx.StaticText(self, label="No file opened")
        self.stats_label = wx.StaticText(self, label="0 entries")

        self.info_sizer.Add(self.file_label, 1, wx.ALIGN_CENTER_VERTICAL)
        self.info_sizer.Add(self.stats_label, 0, wx.ALIGN_CENTER_VERTICAL)

        root.Add(self.info_sizer, 0, wx.EXPAND | wx.ALL, 10)


        self.SetSizer(root)
        self._update_buttons()

    def _bind_events(self) -> None:
        self.Bind(wx.EVT_TOOL, self._on_open, id=wx.ID_OPEN)
        self.Bind(wx.EVT_TOOL, self._on_save_as, id=wx.ID_SAVEAS)
        self.Bind(wx.EVT_TOOL, self._on_close_tool, id=wx.ID_CLOSE)

        self.Bind(wx.EVT_TEXT, self._on_search, self.search_ctrl)
        self.Bind(wx.EVT_TEXT_ENTER, self._on_search, self.search_ctrl)

        self.view.Bind(dv.EVT_DATAVIEW_ITEM_CONTEXT_MENU, self._on_context_menu)
        self.view.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self._on_item_activated)

        self.Bind(wx.EVT_CLOSE, self._on_close)

        # Standard keyboard shortcuts.
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def open_file(self, path: str | Path) -> bool:
        path = Path(path)

        try:
            model = decode_file(path)
        except (OSError, ValueError, LocalizError, UnicodeError) as exc:
            self._show_error("Unable to open localization resource", exc)
            return False

        if self.modified and not self._confirm_discard():
            return False

        self.model = model
        self.source_path = path
        self.output_path = path
        self.modified = False

        self._refresh_view()
        self._update_title()
        return True

    def save_file(self, path: str | Path | None = None) -> bool:
        if self.model is None:
            return False

        target = Path(path) if path else self.output_path

        if target is None:
            return self.save_as_file()

        try:
            encode_file(self.model, target)
        except (OSError, ValueError, LocalizError, UnicodeError) as exc:
            self._show_error("Unable to save localization resource", exc)
            return False

        self.output_path = target
        self.modified = False
        self._update_title()
        self._update_stats()
        return True

    def save_as_file(self) -> bool:
        if self.model is None:
            return False

        default_name = (
            self.output_path.name
            if self.output_path
            else "localization.localiz"
        )

        with wx.FileDialog(
            self,
            "Build Localization Resource",
            wildcard="Localization resource (*.localiz)|*.localiz|All files (*.*)|*.*",
            defaultFile=default_name,
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return False
            target = Path(dlg.GetPath())

        return self.save_file(target)

    # ------------------------------------------------------------------
    # View/model synchronization
    # ------------------------------------------------------------------

    def _refresh_view(self) -> None:
        self._updating_view = True
        try:
            self.view.DeleteAllItems()
            self._visible_indices.clear()

            if self.model is None:
                return

            query = self.search_ctrl.GetValue().strip().casefold()

            for index, entry in enumerate(self.model.entries):
                if query and query not in entry.key.casefold() and query not in entry.value.casefold():
                    continue

                self.view.AppendItem([entry.key, self._display_value(entry.value)])
                self._visible_indices.append(index)
        finally:
            self._updating_view = False

        self._update_stats()
        self._update_buttons()

    @staticmethod
    def _display_value(value: str) -> str:
        """Render multiline values compactly in the table without losing data."""
        return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ↵ ")

    def _update_stats(self) -> None:
        if self.model is None:
            self.stats_label.SetLabel("0 entries")
            return

        shown = len(self._visible_indices)
        total = self.model.key_count

        if shown == total:
            text = f"{total:,} entries"
        else:
            text = f"{shown:,} shown / {total:,} entries"

        self.stats_label.SetLabel(text)
        self.info_sizer.Layout()

    def _update_title(self) -> None:
        name = self.output_path.name if self.output_path else "Untitled"
        marker = " *" if self.modified else ""

        if self.output_path:
            self.file_label.SetLabel(self.output_path.name)
            self.file_label.SetToolTip(str(self.output_path))
        else:
            self.file_label.SetLabel("No file opened")
            self.file_label.SetToolTip("")

    def _update_buttons(self) -> None:
        has_model = self.model is not None
        self.toolbar.EnableTool(wx.ID_SAVEAS, has_model)

    # ------------------------------------------------------------------
    # Editing
    # ------------------------------------------------------------------

    def _selected_model_indices(self) -> list[int]:
        result = []

        for item in self.view.GetSelections():
            row = self.view.ItemToRow(item)
            if 0 <= row < len(self._visible_indices):
                result.append(self._visible_indices[row])

        return result

    def _edit_item(self, item: dv.DataViewItem) -> None:
        if self.model is None or not item or not item.IsOk():
            return

        row = self.view.ItemToRow(item)
        if row < 0 or row >= len(self._visible_indices):
            return

        model_index = self._visible_indices[row]
        entry = self.model.entries[model_index]

        dlg = MultilineValueDialog(self, entry.key, entry.value)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return

            new_value = dlg.value

            try:
                self.model.set(entry.key, new_value, create=False)
            except (ValueError, TypeError, KeyError) as exc:
                self._show_error("Invalid Value", exc)
                return

            self.modified = True
            self._refresh_view()
            self._update_title()

            # Restore selection after refresh.
            if row < self.view.GetItemCount():
                new_item = self.view.RowToItem(row)
                self.view.Select(new_item)
                self.view.EnsureVisible(new_item)
        finally:
            dlg.Destroy()


    # ------------------------------------------------------------------
    # Menus / shortcuts
    # ------------------------------------------------------------------

    def _on_context_menu(self, event: dv.DataViewEvent) -> None:
        menu = wx.Menu()

        edit = menu.Append(wx.ID_EDIT, "Edit Value")
        menu.AppendSeparator()
        copy_key = menu.Append(wx.ID_ANY, "Copy Key")
        copy_value = menu.Append(wx.ID_ANY, "Copy Value")

        item = event.GetItem()
        if not item or not item.IsOk():
            edit.Enable(False)
            copy_key.Enable(False)
            copy_value.Enable(False)

        def on_menu(cmd_event: wx.CommandEvent) -> None:
            item_now = item
            row = self.view.ItemToRow(item_now) if item_now and item_now.IsOk() else -1

            if cmd_event.GetId() == wx.ID_EDIT and row >= 0:
                self._edit_item(item_now)

            elif cmd_event.GetId() == copy_key.Id and row >= 0:
                wx.TheClipboard.Open()
                wx.TheClipboard.SetData(wx.TextDataObject(self.view.GetValue(row, 0)))
                wx.TheClipboard.Close()

            elif cmd_event.GetId() == copy_value.Id and row >= 0:
                wx.TheClipboard.Open()
                wx.TheClipboard.SetData(wx.TextDataObject(self.view.GetValue(row, 1)))
                wx.TheClipboard.Close()

        self.Bind(wx.EVT_MENU, on_menu)

        try:
            self.PopupMenu(menu)
        finally:
            self.Unbind(wx.EVT_MENU, handler=on_menu)
            menu.Destroy()

    def _on_item_activated(self, event: dv.DataViewEvent) -> None:
        self._edit_item(event.GetItem())

    def _on_char_hook(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()

        if event.ControlDown() and key in (ord("O"), ord("o")):
            self._on_open(event)
            return

        if event.ControlDown() and key in (ord("S"), ord("s")):
            if event.ShiftDown():
                self._on_save_as(event)
            else:
                self._on_save(event)
            return

        if event.ControlDown() and key in (ord("F"), ord("f")):
            self.search_ctrl.SetFocus()
            self.search_ctrl.SelectAll()
            return


        event.Skip()

    # ------------------------------------------------------------------
    # Dialog lifecycle
    # ------------------------------------------------------------------

    def _on_close_tool(self, _event: wx.CommandEvent) -> None:
        self.Close()

    def _on_open(self, _event: wx.CommandEvent) -> None:
        with wx.FileDialog(
            self,
            "Open Localization Resource",
            wildcard="Localization resource (*.localiz)|*.localiz|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            self.open_file(dlg.GetPath())

    def _on_save_as(self, _event: wx.CommandEvent) -> None:
        self.save_as_file()

    def _on_search(self, _event: wx.CommandEvent) -> None:
        self._refresh_view()

    def _confirm_discard(self) -> bool:
        if not self.modified:
            return True

        answer = wx.MessageBox(
            "There are unsaved changes.\n\nDiscard them?",
            "Unsaved Changes",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            self,
        )
        return answer == wx.YES

    def _on_close(self, event: wx.CloseEvent) -> None:
        if self._confirm_discard():
            event.Skip()

    def EndModal(self, retCode: int) -> None:
        # If OK is pressed, require a save so the caller receives a valid
        # on-disk resource. Existing unmodified files do not need rewriting.
        if retCode == wx.ID_OK and self.model is not None and self.modified:
            if not self.save_file():
                return
        super().EndModal(retCode)

    def _show_error(self, title: str, exc: Exception) -> None:
        wx.MessageBox(
            str(exc),
            title,
            wx.OK | wx.ICON_ERROR,
            self,
        )


class MultilineValueDialog(wx.Dialog):
    """Dedicated editor for localization values, including multiline text."""

    def __init__(self, parent: wx.Window, key: str, value: str):
        super().__init__(
            parent,
            title=f"Edit Value",
            size=(820, 500),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )

        self.SetSizeHints(wx.Size(500, 300), wx.DefaultSize)

        self.value = value
        
        root = wx.BoxSizer(wx.VERTICAL)

        header = wx.BoxSizer(wx.HORIZONTAL)
        header.Add(
            wx.StaticText(self, label="Key:"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            8,
        )
        key_label = wx.StaticText(self, label=key)
        key_label.SetFont(key_label.GetFont().Bold())
        header.Add(key_label, 1, wx.ALIGN_CENTER_VERTICAL)
        root.Add(header, 0, wx.EXPAND | wx.ALL, 12)

        self.text_ctrl = wx.TextCtrl(
            self,
            value=value,
            style=wx.TE_MULTILINE | wx.TE_RICH2 | wx.HSCROLL,
        )
        root.Add(self.text_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons:
            root.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)

        self.SetSizer(root)

        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

        self.CentreOnParent()
        self.text_ctrl.SetFocus()
        self.text_ctrl.SetInsertionPointEnd()


    def _on_char_hook(self, event: wx.KeyEvent) -> None:
        # Ctrl+Enter accepts the dialog while Enter remains available for
        # inserting line breaks in the multiline editor.
        if event.ControlDown() and event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_ok(event)
            return
        event.Skip()

    def _on_ok(self, _event: wx.CommandEvent) -> None:
        self.value = self.text_ctrl.GetValue()
        self.EndModal(wx.ID_OK)


def open_localiz_editor(
    parent: wx.Window | None,
    filepath: str | Path | None = None,
) -> Optional[Path]:
    """Open the reusable localization editor.

    Returns the saved output path when the user presses OK, otherwise None.
    """
    dlg = LocalizationEditorDialog(parent, filepath)
    try:
        if dlg.ShowModal() == wx.ID_OK:
            return dlg.output_path
        return None
    finally:
        dlg.Destroy()
