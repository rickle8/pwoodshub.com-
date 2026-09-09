"""
local_config.py — private credentials. Do NOT share or commit this file.
(.gitignore already excludes it if this folder ever becomes a git repo.)
"""

# ESPN session cookies (used by scraper.py for the 2014-2024 ESPN seasons).
# Grab fresh values from browser dev tools on espn.com if these expire.
ESPN_SWID = "{903ae7c0-152e-47ad-bea0-5687394e70e4}"
ESPN_S2   = "AEC6yNOCVgO57OcmEA2X9IvSO1n82PucryWA1Tyl7vL0vhEB13P1zVDThUGlTEY0lp2tO6KaXnuwY%2FE0la36PcJO9smK3EeRA%2FlqK5nErEIic2g6SqdaByQQVy9fUG0swQDQMAKeCCwxzWll4K3FxTdwrfvDLTz%2FICycfI1tzun6w0gT1Y%2BAcKDQ0MYwk%2BJCAn4dpPWWECOBq1meiagzmFWbJNMPB5VpwKXV5x72Pa92rEWZcJtidU1O3RW6Rs7G4%2B6tc8ky8Sw1IbcNmqM%2FyKrB"

# Klipy GIF API key — powers the chat GIF picker.
# Get a free lifetime key: https://klipy.com → Partner Panel → API Keys.
# Leave empty to hide the GIF button in chat entirely.
KLIPY_API_KEY = ""
