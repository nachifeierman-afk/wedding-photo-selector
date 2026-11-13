import sys
import sqlite3
import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QKeySequence, QShortcut, QImageReader
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QLabel,
    QPushButton,
    QFileDialog,
    QVBoxLayout,
    QHBoxLayout,
    QComboBox,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QStackedWidget,
    QListWidget,
    QListWidgetItem,
    QInputDialog,
)


APP_DATA = Path.home() / "WeddingPhotoSelectorData"
APP_DATA.mkdir(exist_ok=True)

DATABASE = APP_DATA / "photos.db"

SUPPORTED_FORMATS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

START_PAGE = 0
ALBUM_PAGE = 1

DEFAULT_GOAL = 50


class WeddingPhotoSelector(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Wedding Photo Selector")
        self.resize(1200, 850)

        self.current_album_id = None
        self.current_album_name = ""
        self.photos = []
        self.current_index = 0
        self.shortcuts = []

        self.connect_database()

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.stack.addWidget(self.build_start_page())
        self.stack.addWidget(self.build_album_page())

        self.show_start_screen()

    # -----------------------------
    # DATABASE
    # -----------------------------

    def connect_database(self):
        self.db = sqlite3.connect(DATABASE)

        self.db.execute("""
            CREATE TABLE IF NOT EXISTS albums (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                target INTEGER
            )
        """)

        # Albums made by an older version have no goal column yet
        album_columns = [
            row[1] for row in self.db.execute("PRAGMA table_info(albums)")
        ]
        if "target" not in album_columns:
            self.db.execute("ALTER TABLE albums ADD COLUMN target INTEGER")

        photo_columns = [
            row[1] for row in self.db.execute("PRAGMA table_info(photos)")
        ]

        # Oldest version of the app: photos had no album. Move them into
        # an album called "My Photos" so nothing is lost.
        if photo_columns and "album_id" not in photo_columns:
            self.migrate_old_data()

        self.create_tables()
        self.db.commit()

    def create_tables(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS photos (
                album_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                PRIMARY KEY (album_id, path)
            )
        """)

        self.db.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id INTEGER,
                path TEXT,
                old_status TEXT,
                new_status TEXT
            )
        """)

    def migrate_old_data(self):
        self.db.execute("ALTER TABLE photos RENAME TO photos_old")
        self.db.execute("DROP TABLE IF EXISTS history")
        self.create_tables()

        old_count = self.db.execute(
            "SELECT COUNT(*) FROM photos_old"
        ).fetchone()[0]

        if old_count:
            cursor = self.db.execute(
                "INSERT INTO albums (name) VALUES (?)", ("My Photos",)
            )
            self.db.execute(
                """
                INSERT INTO photos (album_id, path, status)
                SELECT ?, path, status FROM photos_old
                """,
                (cursor.lastrowid,),
            )

        self.db.execute("DROP TABLE photos_old")

    def get_target(self, album_id):
        row = self.db.execute(
            "SELECT target FROM albums WHERE id = ?", (album_id,)
        ).fetchone()
        return row[0] if row else None

    def chosen_count(self, album_id):
        """Photos chosen for the album: Saved plus Favorites."""
        return self.db.execute(
            """
            SELECT COUNT(*) FROM photos
            WHERE album_id = ? AND status IN ('kept', 'favorite')
            """,
            (album_id,),
        ).fetchone()[0]

    # -----------------------------
    # START SCREEN
    # -----------------------------

    def build_start_page(self):

        page = QWidget()

        layout = QVBoxLayout(page)
        layout.setContentsMargins(60, 40, 60, 40)
        layout.setSpacing(18)

        title = QLabel("Wedding Photo Selector")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 36px; font-weight: bold;")
        layout.addWidget(title)

        subtitle = QLabel("Choose an album, or create a new one")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("font-size: 16px; color: gray;")
        layout.addWidget(subtitle)

        self.album_list = QListWidget()
        self.album_list.setStyleSheet("""
            QListWidget { font-size: 18px; border-radius: 10px; padding: 6px; }
            QListWidget::item { padding: 14px; }
        """)
        self.album_list.itemDoubleClicked.connect(
            lambda item: self.open_selected_album()
        )
        layout.addWidget(self.album_list, 1)

        self.empty_hint = QLabel(
            "No albums yet. Click \"+ New Album\" to create your first one."
        )
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setStyleSheet("font-size: 16px; color: gray;")
        layout.addWidget(self.empty_hint)

        buttons = QHBoxLayout()

        new_button = QPushButton("+ New Album")
        open_button = QPushButton("Open")
        goal_button = QPushButton("Change Goal")
        rename_button = QPushButton("Rename")
        delete_button = QPushButton("Delete")

        new_button.clicked.connect(self.new_album)
        open_button.clicked.connect(self.open_selected_album)
        goal_button.clicked.connect(self.change_goal)
        rename_button.clicked.connect(self.rename_album)
        delete_button.clicked.connect(self.delete_album)

        for button in [
            new_button, open_button, goal_button, rename_button, delete_button
        ]:
            button.setMinimumHeight(50)
            button.setStyleSheet("font-size: 16px; padding: 8px;")
            buttons.addWidget(button)

        layout.addLayout(buttons)

        return page

    def show_start_screen(self):
        self.current_album_id = None
        self.current_album_name = ""
        self.photos = []

        for shortcut in self.shortcuts:
            shortcut.setEnabled(False)

        self.refresh_albums()
        self.stack.setCurrentIndex(START_PAGE)
        self.setWindowTitle("Wedding Photo Selector")

    def refresh_albums(self):

        rows = self.db.execute(
            """
            SELECT a.id,
                   a.name,
                   a.target,
                   COUNT(p.path),
                   SUM(CASE WHEN p.status IN ('kept', 'favorite')
                            THEN 1 ELSE 0 END),
                   SUM(CASE WHEN p.status != 'pending' THEN 1 ELSE 0 END)
            FROM albums a
            LEFT JOIN photos p ON p.album_id = a.id
            GROUP BY a.id
            ORDER BY a.name COLLATE NOCASE
            """
        ).fetchall()

        self.album_list.clear()

        for album_id, name, target, total, chosen, reviewed in rows:

            chosen = chosen or 0
            reviewed = reviewed or 0

            if total == 0:
                detail = "empty - open it and import a folder"
            else:
                parts = []
                if target:
                    parts.append(f"{chosen:,} of {target:,} chosen")
                else:
                    parts.append(f"{chosen:,} chosen")
                parts.append(f"{reviewed:,} of {total:,} reviewed")
                detail = "     |     ".join(parts)

            item = QListWidgetItem(f"{name}     -     {detail}")
            item.setData(Qt.ItemDataRole.UserRole, album_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, name)
            self.album_list.addItem(item)

        if rows:
            self.album_list.setCurrentRow(0)

        self.empty_hint.setVisible(not rows)

    def selected_album(self):
        item = self.album_list.currentItem()

        if item is None:
            QMessageBox.information(
                self, "Choose an album", "Click an album in the list first."
            )
            return None

        return (
            item.data(Qt.ItemDataRole.UserRole),
            item.data(Qt.ItemDataRole.UserRole + 1),
        )

    def new_album(self):

        name, ok = QInputDialog.getText(self, "New Album", "Album name:")

        if not ok or not name.strip():
            return

        name = name.strip()

        exists = self.db.execute(
            "SELECT 1 FROM albums WHERE name = ?", (name,)
        ).fetchone()

        if exists:
            QMessageBox.warning(
                self, "Name in use", f"An album named \"{name}\" already exists."
            )
            return

        goal, ok = QInputDialog.getInt(
            self,
            "Album Size",
            "How many pictures do you want in your album?",
            DEFAULT_GOAL,
            1,
            100000,
            1,
        )

        if not ok:
            return

        cursor = self.db.execute(
            "INSERT INTO albums (name, target) VALUES (?, ?)", (name, goal)
        )
        self.db.commit()

        self.open_album(cursor.lastrowid, name)

    def change_goal(self):
        selected = self.selected_album()

        if not selected:
            return

        album_id, name = selected

        current = self.get_target(album_id) or DEFAULT_GOAL

        goal, ok = QInputDialog.getInt(
            self,
            "Album Size",
            f"How many pictures do you want in \"{name}\"?",
            current,
            1,
            100000,
            1,
        )

        if not ok:
            return

        self.db.execute(
            "UPDATE albums SET target = ? WHERE id = ?", (goal, album_id)
        )
        self.db.commit()

        self.refresh_albums()

    def open_selected_album(self):
        selected = self.selected_album()

        if selected:
            self.open_album(*selected)

    def rename_album(self):
        selected = self.selected_album()

        if not selected:
            return

        album_id, old_name = selected

        name, ok = QInputDialog.getText(
            self, "Rename Album", "New name:", text=old_name
        )

        if not ok or not name.strip():
            return

        try:
            self.db.execute(
                "UPDATE albums SET name = ? WHERE id = ?",
                (name.strip(), album_id),
            )
            self.db.commit()
        except sqlite3.IntegrityError:
            QMessageBox.warning(
                self, "Name in use",
                f"An album named \"{name.strip()}\" already exists.",
            )
            return

        self.refresh_albums()

    def delete_album(self):
        selected = self.selected_album()

        if not selected:
            return

        album_id, name = selected

        answer = QMessageBox.question(
            self,
            "Delete Album",
            f"Delete the album \"{name}\"?\n\n"
            "This removes the album and your Save/Reject/Favorite choices "
            "for it. Your actual photo files are not touched.",
        )

        if answer != QMessageBox.StandardButton.Yes:
            return

        self.db.execute("DELETE FROM photos WHERE album_id = ?", (album_id,))
        self.db.execute("DELETE FROM history WHERE album_id = ?", (album_id,))
        self.db.execute("DELETE FROM albums WHERE id = ?", (album_id,))
        self.db.commit()

        self.refresh_albums()

    def open_album(self, album_id, name):
        self.current_album_id = album_id
        self.current_album_name = name

        self.album_title.setText(name)
        self.setWindowTitle(f"Wedding Photo Selector - {name}")

        for shortcut in self.shortcuts:
            shortcut.setEnabled(True)

        self.stack.setCurrentIndex(ALBUM_PAGE)

        self.filter_menu.blockSignals(True)
        self.filter_menu.setCurrentText("Pending")
        self.filter_menu.blockSignals(False)

        self.load_photos()

    # -----------------------------
    # ALBUM PAGE (PHOTO SORTING)
    # -----------------------------

    def build_album_page(self):

        page = QWidget()

        main_layout = QVBoxLayout(page)
        main_layout.setContentsMargins(25, 20, 25, 20)
        main_layout.setSpacing(15)

        # Header with back button

        header = QHBoxLayout()

        back_button = QPushButton("< All Albums")
        back_button.setMinimumHeight(40)
        back_button.setStyleSheet("font-size: 15px; padding: 6px 14px;")
        back_button.clicked.connect(self.show_start_screen)

        self.album_title = QLabel("")
        self.album_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.album_title.setStyleSheet("font-size: 28px; font-weight: bold;")

        spacer = QWidget()
        spacer.setFixedWidth(back_button.sizeHint().width())

        header.addWidget(back_button)
        header.addWidget(self.album_title, 1)
        header.addWidget(spacer)

        main_layout.addLayout(header)

        self.progress_text = QLabel("")
        self.progress_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self.progress_text)

        self.progress_bar = QProgressBar()
        main_layout.addWidget(self.progress_bar)

        # Photo display

        self.image_label = QLabel("Click \"Import Photo Folder\" to add photos")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumHeight(450)
        self.image_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )
        self.image_label.setStyleSheet("""
            background-color: #202020;
            color: white;
            border-radius: 12px;
            font-size: 18px;
        """)
        main_layout.addWidget(self.image_label, 1)

        # Filter menu

        filter_layout = QHBoxLayout()

        filter_label = QLabel("Show:")
        self.filter_menu = QComboBox()
        self.filter_menu.addItems([
            "Pending",
            "Saved",
            "Favorites",
            "Rejected",
            "All Photos",
        ])
        self.filter_menu.currentTextChanged.connect(self.change_filter)

        filter_layout.addWidget(filter_label)
        filter_layout.addWidget(self.filter_menu)
        filter_layout.addStretch()

        main_layout.addLayout(filter_layout)

        # Decision buttons

        button_layout = QHBoxLayout()

        self.reject_button = QPushButton("✕ Reject")
        self.keep_button = QPushButton("✓ Save")
        self.favorite_button = QPushButton("★ Favorite")
        self.undo_button = QPushButton("↶ Undo")

        self.reject_button.clicked.connect(lambda: self.make_decision("rejected"))
        self.keep_button.clicked.connect(lambda: self.make_decision("kept"))
        self.favorite_button.clicked.connect(lambda: self.make_decision("favorite"))
        self.undo_button.clicked.connect(self.undo)

        self.reject_button.setStyleSheet("background-color: #b94a48; color: white;")
        self.keep_button.setStyleSheet("background-color: #32865b; color: white;")
        self.favorite_button.setStyleSheet("background-color: #b18b32; color: white;")

        for button in [
            self.reject_button,
            self.keep_button,
            self.favorite_button,
            self.undo_button,
        ]:
            button.setMinimumHeight(55)
            button.setStyleSheet(
                button.styleSheet()
                + "font-size: 16px; padding: 8px; border-radius: 8px;"
            )
            button_layout.addWidget(button)

        main_layout.addLayout(button_layout)

        # Bottom buttons

        bottom_layout = QHBoxLayout()

        import_button = QPushButton("Import Photo Folder")
        export_button = QPushButton("Export Selections")

        import_button.clicked.connect(self.import_folder)
        export_button.clicked.connect(self.export_photos)

        bottom_layout.addWidget(import_button)
        bottom_layout.addWidget(export_button)

        main_layout.addLayout(bottom_layout)

        # Keep keyboard focus off the controls so arrow keys always
        # trigger the shortcuts (and never change the dropdown).
        for widget in [
            back_button,
            self.filter_menu,
            self.reject_button,
            self.keep_button,
            self.favorite_button,
            self.undo_button,
            import_button,
            export_button,
        ]:
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # Keyboard controls (only active while an album is open, so the
        # arrow keys still work normally in the album list)

        for key, action in [
            ("Right", lambda: self.make_decision("kept")),
            ("Left", lambda: self.make_decision("rejected")),
            ("Up", lambda: self.make_decision("favorite")),
            ("Ctrl+Z", self.undo),
        ]:
            shortcut = QShortcut(QKeySequence(key), self, activated=action)
            shortcut.setEnabled(False)
            self.shortcuts.append(shortcut)

        return page

    # -----------------------------
    # IMPORT PHOTOS
    # -----------------------------

    def import_folder(self):

        if self.current_album_id is None:
            return

        folder = QFileDialog.getExistingDirectory(
            self, "Choose Wedding Photo Folder"
        )

        if not folder:
            return

        found = 0
        added = 0

        for photo in Path(folder).rglob("*"):

            if photo.is_file() and photo.suffix.lower() in SUPPORTED_FORMATS:

                cursor = self.db.execute(
                    """
                    INSERT OR IGNORE INTO photos (album_id, path, status)
                    VALUES (?, ?, 'pending')
                    """,
                    (self.current_album_id, str(photo.resolve())),
                )

                found += 1
                added += cursor.rowcount

        self.db.commit()

        self.filter_menu.blockSignals(True)
        self.filter_menu.setCurrentText("Pending")
        self.filter_menu.blockSignals(False)

        self.load_photos()

        QMessageBox.information(
            self,
            "Import Complete",
            f"Found {found} supported image files.\n"
            f"{added} new, {found - added} already in this album.",
        )

    # -----------------------------
    # LOAD PHOTOS
    # -----------------------------

    def change_filter(self, *args):
        self.load_photos()

    def load_photos(self, index=0):

        if self.current_album_id is None:
            return

        filter_name = self.filter_menu.currentText()

        status_map = {
            "Pending": "pending",
            "Saved": "kept",
            "Favorites": "favorite",
            "Rejected": "rejected",
        }

        if filter_name in status_map:
            rows = self.db.execute(
                """
                SELECT path FROM photos
                WHERE album_id = ? AND status = ?
                ORDER BY path
                """,
                (self.current_album_id, status_map[filter_name]),
            ).fetchall()
        else:
            rows = self.db.execute(
                """
                SELECT path FROM photos
                WHERE album_id = ?
                ORDER BY path
                """,
                (self.current_album_id,),
            ).fetchall()

        self.photos = [
            Path(row[0]) for row in rows if Path(row[0]).is_file()
        ]

        self.current_index = index if index < len(self.photos) else 0

        self.show_current_photo()
        self.update_progress()

    # -----------------------------
    # DISPLAY PHOTO
    # -----------------------------

    def show_current_photo(self):

        if self.current_album_id is None:
            return

        if not self.photos:
            self.image_label.setPixmap(QPixmap())

            total = self.db.execute(
                "SELECT COUNT(*) FROM photos WHERE album_id = ?",
                (self.current_album_id,),
            ).fetchone()[0]

            if total == 0:
                self.image_label.setText(
                    "This album is empty.\n"
                    "Click \"Import Photo Folder\" to add photos."
                )
            else:
                self.image_label.setText("No photos in this category")

            return

        if self.current_index >= len(self.photos):
            self.current_index = 0

        photo = self.photos[self.current_index]

        reader = QImageReader(str(photo))
        reader.setAutoTransform(True)  # applies EXIF rotation
        image = reader.read()

        if image.isNull():
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(
                f"Could not display:\n{photo.name}\n\n{reader.errorString()}"
            )
            return

        pixmap = QPixmap.fromImage(image)

        scaled = pixmap.scaled(
            self.image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        self.image_label.setPixmap(scaled)
        self.image_label.setToolTip(str(photo))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.show_current_photo()

    # -----------------------------
    # MAKE DECISION
    # -----------------------------

    def make_decision(self, new_status):

        if self.current_album_id is None or not self.photos:
            return

        idx = self.current_index
        photo = self.photos[idx]
        path = str(photo)

        result = self.db.execute(
            "SELECT status FROM photos WHERE album_id = ? AND path = ?",
            (self.current_album_id, path),
        ).fetchone()

        if not result:
            return

        old_status = result[0]
        chosen_before = self.chosen_count(self.current_album_id)

        self.db.execute(
            """
            INSERT INTO history (album_id, path, old_status, new_status)
            VALUES (?, ?, ?, ?)
            """,
            (self.current_album_id, path, old_status, new_status),
        )

        self.db.execute(
            "UPDATE photos SET status = ? WHERE album_id = ? AND path = ?",
            (new_status, self.current_album_id, path),
        )

        self.db.commit()

        self.load_photos(index=idx)

        # If the photo is still in this view (e.g. "All Photos"),
        # move on to the next one.
        if self.photos and self.photos[self.current_index] == photo:
            self.current_index = (idx + 1) % len(self.photos)
            self.show_current_photo()

        # Let the user know when they reach their album size
        target = self.get_target(self.current_album_id)
        chosen_after = self.chosen_count(self.current_album_id)

        if target and chosen_before < target <= chosen_after:
            QMessageBox.information(
                self,
                "Album Complete",
                f"You've chosen {target:,} photos for this album - "
                "that's your goal!\n\n"
                "You can keep going, or use Export Selections when you're "
                "ready. (Change the goal any time from the album list.)",
            )

    # -----------------------------
    # UNDO
    # -----------------------------

    def undo(self):

        if self.current_album_id is None:
            return

        result = self.db.execute(
            """
            SELECT id, path, old_status
            FROM history
            WHERE album_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (self.current_album_id,),
        ).fetchone()

        if not result:
            return

        history_id, path, old_status = result

        self.db.execute(
            "UPDATE photos SET status = ? WHERE album_id = ? AND path = ?",
            (old_status, self.current_album_id, path),
        )

        self.db.execute("DELETE FROM history WHERE id = ?", (history_id,))

        self.db.commit()

        self.load_photos()

        # Jump back to the photo that was just restored
        restored = Path(path)
        if restored in self.photos:
            self.current_index = self.photos.index(restored)
            self.show_current_photo()

    # -----------------------------
    # PROGRESS
    # -----------------------------

    def update_progress(self):

        rows = self.db.execute(
            """
            SELECT status, COUNT(*)
            FROM photos
            WHERE album_id = ?
            GROUP BY status
            """,
            (self.current_album_id,),
        ).fetchall()

        counts = {"pending": 0, "kept": 0, "favorite": 0, "rejected": 0}

        for status, count in rows:
            counts[status] = count

        total = sum(counts.values())
        chosen = counts["kept"] + counts["favorite"]
        target = self.get_target(self.current_album_id)

        text = (
            f"Saved: {counts['kept']:,}     "
            f"Favorites: {counts['favorite']:,}     "
            f"Rejected: {counts['rejected']:,}     "
            f"Left to review: {counts['pending']:,}"
        )

        if target:
            if chosen < target:
                goal_text = f"Chosen: {chosen:,} of {target:,}  ({target - chosen:,} to go)"
            elif chosen == target:
                goal_text = f"Chosen: {chosen:,} of {target:,}  - goal reached!"
            else:
                goal_text = (
                    f"Chosen: {chosen:,} of {target:,}  "
                    f"({chosen - target:,} over your goal)"
                )

            self.progress_text.setText(f"{goal_text}\n{text}")

            self.progress_bar.setMaximum(target)
            self.progress_bar.setValue(min(chosen, target))
            self.progress_bar.setFormat("%v of %m chosen")
        else:
            self.progress_text.setText(
                f"Chosen: {chosen:,}     {text}"
            )

            self.progress_bar.setMaximum(max(total, 1))
            self.progress_bar.setValue(total - counts["pending"])
            self.progress_bar.setFormat("%v of %m reviewed")

    # -----------------------------
    # EXPORT PHOTOS
    # -----------------------------

    def export_photos(self):

        if self.current_album_id is None:
            return

        destination = QFileDialog.getExistingDirectory(
            self, "Choose Export Destination"
        )

        if not destination:
            return

        destination = Path(destination)

        rows = self.db.execute(
            """
            SELECT path, status FROM photos
            WHERE album_id = ? AND status IN ('kept', 'favorite')
            """,
            (self.current_album_id,),
        ).fetchall()

        copied = 0
        errors = []

        for path_string, status in rows:

            source = Path(path_string)

            if not source.is_file():
                continue

            folder_name = "Favorites" if status == "favorite" else "Saved"

            export_folder = destination / folder_name
            export_folder.mkdir(parents=True, exist_ok=True)

            target = export_folder / source.name

            # Avoid overwriting files with identical names.
            counter = 1
            while target.exists():
                target = export_folder / f"{source.stem}_{counter}{source.suffix}"
                counter += 1

            try:
                shutil.copy2(source, target)
                copied += 1
            except OSError as error:
                errors.append(f"{source.name}: {error}")

        message = f"Exported {copied} photos."

        if errors:
            message += (
                f"\n\n{len(errors)} files could not be copied. "
                "Check that the source drive is connected."
            )

        QMessageBox.information(self, "Export Complete", message)

    # -----------------------------
    # CLOSE APPLICATION
    # -----------------------------

    def closeEvent(self, event):
        self.db.close()
        event.accept()


if __name__ == "__main__":

    app = QApplication(sys.argv)

    window = WeddingPhotoSelector()
    window.show()

    sys.exit(app.exec())