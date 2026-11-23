# Changelog

Scattered Showers, newest first. Version numbers: the first number changes for a rebuild, the middle one when something new is added, the last one for fixes.

## 1.2.0 (2026-11-23)

New
- **Landmarks in the banner for eleven big cities.** Chicago (Hancock and Sears Tower), New York (Empire State Building and One World Trade), Los Angeles (U.S. Bank Tower), San Francisco (Transamerica Pyramid), Washington DC (Washington Monument and the Capitol, in a low skyline because DC has a height limit), Dallas (Reunion Tower), San Antonio (Tower of the Americas) and Seattle (Space Needle) and St. Louis (the Gateway Arch, standing in front of a lower skyline because St. Louis does not have a tall one). Each one is drawn from real night photos, sits inside the skyline with the base hidden behind the front row, and lights up at night with a red beacon on top. A place gets them when the page calls it a city (100,000 people or more) and it is within a set distance of the city's center (15 miles for most, less for Dallas, San Francisco, Seattle, DC and St. Louis so the next town over keeps its own skyline). This works for the three stations and for Current and Custom locations.
- **The Las Vegas Strip in the banner:** the STRAT tower, New York-New York (with its little Statue of Liberty), Excalibur, the Eiffel Tower, the Sphere, the High Roller wheel and the Luxor pyramid, all lit up at night (the Luxor beam, the colored wheel and Sphere). Shows for Las Vegas, Paradise (where the Strip really is), Henderson, North Las Vegas and the rest of the valley.
- **Desert places look like desert.** Phoenix, Tucson, Las Vegas, Albuquerque, El Paso, Las Cruces, Palm Springs, Sedona, St. George, Yuma, Lake Havasu City and a few more get tan ground, their own mountains behind them (Palm Springs has one tall wall with wind turbines, Albuquerque is tallest on the east side, Sedona and St. George have red cliffs and mesas), desert plants (saguaro, palms, yucca, Joshua trees) and adobe-colored houses with flat roofs. This works for stations, Current location and Custom location, in cities, towns and open country.
- **Desert extras:** Camelback Mountain behind Phoenix, the lit star on the Franklin Mountains at El Paso, hot air balloons over Albuquerque (they stay down in storms, fog and wind and glow at night), the Aerial Tramway cable on the Palm Springs mountain wall, the Chapel of the Holy Cross in Sedona, and desert-style town centers (palms and flat mid-century blocks in Palm Springs, stepped adobe in Sedona).
- **Kingsburg's water tower is a coffee pot,** because it is one. The Kingsburg station gets it, and so does Current or Custom location anywhere within 3 miles of the middle of Kingsburg, CA.

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
