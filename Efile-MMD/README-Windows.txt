# E-file MMD for Windows

## Start

Double-click `EFileMMD.exe`. A console window will show the local address and the app will open in your default browser. Keep the console window open while using the app. Close it with Ctrl+C to stop the server.

The app runs only on this computer at `http://127.0.0.1:8765` (or the next available nearby port). This build does not enable network sharing.

## Library data and backup

On first launch, the included example library is copied to `%LOCALAPPDATA%\EFileMMD\data`. Uploaded files, the SQLite database, and settings are saved there on subsequent launches. Back up that whole `data` folder while the app is stopped. Removing the executable does not remove the library.

## Notes

This is a Windows 64-bit executable. It packages the local SQLite version of the app. The optional PostgreSQL and SMB storage integrations in the original project are designed for macOS and are not configured in this Windows build.
