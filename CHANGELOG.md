# Changelog

Scattered Showers, newest first. Version numbers: the first number changes for a rebuild, the middle one when something new is added, the last one for fixes.

## 1.1.1 (2026-11-22)

- The line under the page title now says "or any place you pick" (on its own line on a phone), so visitors can tell the page is not limited to the three stations. The README intro and the installed app's description match.

## 1.1.0 (2026-11-22)

New
- **Current location and Custom location.** Type an address, city, zip code or airport code, or let the browser find you, and get conditions, forecast, radar and a banner for that spot anywhere in the US. Nothing you type passes through a server of mine.
- **Sky banner** at the top of each station page and each custom place: a drawn town that matches the place (terrain, water, size of town, time of day, season) with the real sky, rain, wind, fog, snow and thunder. Rain and wind come from the station's own gauge and sensors; the sky comes from the nearest airport reports, and the page says so when something comes from an airport or from radar.
- **Hourly forecast redrawn** as a temperature line with a weather icon per hour. The line and the temperature and rain-chance numbers use the same colors as the Temperature gauge. Streaks beside an icon mean wind of 15 mph or more that hour.
- **Temperature colors** now run in eight bands (violet below 10, indigo, blue, blue-green, green, yellow, orange, red above 90) instead of four. This changes the gauge, the hourly line and the big numbers on the overview page. The brown that used to show up in the 60s on the light theme is gone.
- **Credits** at the bottom of the page for every data source and code library, and a fuller README.
- Version number in the page footer, and this changelog.


Fixes
- Phone layout for long station names: "House of / Shih Tzus / Elgin, TX" instead of "House of Shih / Tzus".
- Custom places now take their sky words from the newest airport report, not the newest full one, which had been up to an hour behind (Fort Wayne showed "Mostly Cloudy" while the station said "Partly Cloudy").
- Airport data up to 75 minutes old is trusted on custom places (it was 60), so rain, snow and fog no longer blank out for a stretch of every hour.

Known rough edges
- The NOAA radar layer that tells rain from snow can call snow "rain" right at the freezing line, so the radar map may paint light snow in rain colors near 33°F. Decided to leave it.
- Hourly wind streaks use sustained wind only. The forecast feed has no gusts.

## 1.0.0 (2026-10-29, first public release)

- Three stations (Fort Wayne, Elgin, Kingsburg) with current conditions, gauges and history charts with a Chart/Table switch.
- 5-day and hourly forecasts and active alerts from the National Weather Service.
- Radar map with a snowfall estimate, record highs and lows, a station health table, light and dark themes, a How To Guide, and an installable home-screen app.
