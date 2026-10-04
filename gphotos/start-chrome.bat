@echo off
rem Optional: opens Chrome on the same profile (gphotos\chrome-profile) so you can sign in to Google Photos by hand.
rem Close this Chrome window before running gphotos.js: it launches its own Chrome on that profile
rem (it does NOT attach to the debug port below), and two Chromes cannot share one profile.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="%~dp0chrome-profile" --remote-debugging-port=9222 --no-first-run https://photos.google.com/
