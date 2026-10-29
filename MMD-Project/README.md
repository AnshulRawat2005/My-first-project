# e-file MMD · Local document register

A small, offline-first document library for macOS. The app runs in your browser and can be shared with other devices on the same trusted private network. There is no cloud service or individual sign-in. The Mac running the app is the shared server and must remain on with its Terminal window open.

## Start it

1. Install Python 3 if it is not already available on your Mac. macOS may offer to install its command line tools the first time Python is requested.
2. **Run** `run.command`; do not open it in a text editor. In Terminal, navigate to this folder and run `./run.command`, or double-click the file in Finder. If macOS blocks it, right-click the file and choose **Open** once.
3. The app opens on this Mac at `http://127.0.0.1:8765`. Terminal also prints a **Same-network users** address. Share that address with users on the same trusted Wi-Fi/LAN. Keep the Terminal window open and the Mac awake while they use it. Press Control-C to stop the app. If the default port is occupied, the app tries the next available port and prints the updated addresses.

Alternatively, open Terminal in this folder and run `python3 server.py`. If port 8765 is occupied, use `python3 server.py --port 8766` and open the address printed in Terminal.

## Optional network storage

SQLite and local file storage work without extra packages. For a remote PostgreSQL database, install the optional connector and macOS Keychain integration once from this folder:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements-network.txt
```

`run.command` uses this project-local Python environment automatically when it exists, so the optional PostgreSQL packages are available to the running app without changing macOS's system Python.

Then open **Storage settings** in the app. PostgreSQL needs a reachable server IP/host, port (normally 5432), database, username, password, and permission to create tables and indexes. The app tests the connection before switching. The password is kept in macOS Keychain; it is not written to the settings file. Local SQLite records are copied to PostgreSQL by document ID when switching. Back up the PostgreSQL database separately.

For SMB files, choose **Connect in Finder**, enter the SMB server IP/host and share, then sign in through macOS. Finder/Keychain handles SMB credentials. After the share mounts, choose **SMB shared folder** in Storage settings and save. New uploads are written to `e-file MMD/documents` on that share. Existing files remain at their original paths. Back up the SMB share separately.

The PostgreSQL and SMB connections are made by the Mac running this app. Every connected user sees the same documents and classification masters. Keep the database and file share reachable from that Mac. The server accepts requests from devices on the LAN without individual accounts, so use it only on a trusted private network and do not forward its port to the public internet. The operating system firewall may ask you to allow incoming connections; allow Python on your private network.

## Use it

- Use the left navigation to switch between **Home**, **Upload**, and **View Files**. Home shows the library count and storage used. Upload is a full-page form modeled on the supplied MMD project mockup; View Files has a search bar and classification/tag filters.
- Open **Settings → Classification masters** to add or remove reusable values for Unit, Category, Sub-category, Document Type, Item Category, Department, and Financial Year. Save the master list to make those values available in the upload form. Values already used by documents remain available in the form and filters.
- Add PDF, DOC, DOCX, PNG, JPG/JPEG, GIF, WebP, BMP, or TIFF files up to **5 MB**. The app checks the extension, size, and known file signatures for PDFs and images before saving.
- PDF and image contents preview in the upload dialog. Word files show a pre-save file summary; after saving, use **Open** to view them in the Mac app associated with Word documents. Word rendering depends on an installed compatible app.
- Enter tags and press Return. Existing tags appear as suggestions. Tags are case-insensitive for reuse. Use multiple tags to assign a combination to a document.
- Select multiple tag chips or enter a comma-separated list in search. A document appears only when it has **every** selected tag. Documents can share any tag combination.
- Classify each document by Unit, Category, Sub-category, Document Type, Item Category, Department, and Financial Year. The example values from the project mockup are available as suggestions; values from existing documents become suggestions too. Enter a comma-separated combination of exact tags or classification values to filter the library.
- Use **Preview** for an inline PDF/image preview, or **Open** to hand the attachment to your browser or associated app.
- The dashboard reports total document storage in MB or GB. **Activity logs** shows timestamped failures and lets you copy them. Client-side failures are kept in this browser; server-side upload failures are saved in the local database.
- If the page says it cannot reach the local app, check that the Terminal window running it is still open. Restart `run.command` and use the address it opens.
- If Storage settings reports that the server is out of date, stop the older app with Control-C in its Terminal window, run `run.command` again, and open the exact address printed there. When port 8765 is already occupied, the updated app selects and opens another local port.

## Local data and backup

With local settings, documents live in `data/documents/`; the SQLite index and tags live in `data/efile.sqlite3`. Back up the whole `data/` folder while the app is stopped. When PostgreSQL or SMB is selected, back up those services separately as well. Do not edit app data files by hand. Removing the app folder removes the local library, so keep separate backups if the records matter.

This is intended for one local user on a trusted Mac. It does not encrypt files at rest; use macOS FileVault and a protected user account for device-level protection. The app does not upload data, sync across devices, or provide multi-user access.
