@echo off
rem Starts a visible Chrome with its own profile and a debug port. Sign in to Google Photos here,
rem then leave it open: gphotos.js attaches to it on port 9222.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="%~dp0chrome-profile" --remote-debugging-port=9222 --no-first-run https://photos.google.com/
