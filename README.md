# Fuel Optimiser

Finds the mathematically cheapest place to fill up, either near you or along a route.

* **Data:** Official UK Government "Fuel Finder" scheme (every UK forecourt must report price changes within 30 minutes), [Fuel Finder Developer Portal](https://www.fuel-finder.service.gov.uk).

* **Routing:** OSRM public demo server.

* **Geocoding:** postcodes.io / Nominatim.

* **Map:** Leaflet + OpenStreetMap tiles.

## Setup

1. **Install Dependencies**

   ```
   pip install requests python-dotenv
   
   ```

2. **Get API Credentials**
   Register for free API credentials (requires a GOV.UK One Login) at [developer.fuel-finder.service.gov.uk](https://developer.fuel-finder.service.gov.uk).

3. **Configure Environment Variables**
   Create a `.env` file in the same folder as the script:

   ```
   FUEL_FINDER_CLIENT_ID=your_actual_client_id
   FUEL_FINDER_CLIENT_SECRET=your_actual_client_secret
   
   ```

   *(Alternatively, export them directly in your terminal via `export FUEL_FINDER_CLIENT_ID="..."` on Linux/macOS or `$env:FUEL_FINDER_CLIENT_ID="..."` on Windows PowerShell).*

4. **Run the Application**

   ```
   python fuel_optimiser.py
   
   ```

   This automatically opens `http://localhost:8000` in your browser.

## The Maths

A cheaper station is only cheaper if the extra driving doesn't eat the saving. Fuel burned on the detour has to be bought too, so for a fill of $V$ litres:

$$
\text{detour\_litres} = \frac{\text{detour\_miles}}{\text{mpg}} \times 4.54609
$$

$$
\text{true\_cost} = \text{price\_per\_litre} \times (V + \text{detour\_litres})
$$

$$
\text{effective p/L} = \frac{\text{true\_cost}}{V}
$$

Stations are ranked by `true_cost` (lowest first).

* **Nearby mode:** Detour = round trip from your location (real road distance).

* **Route mode:** Detour = extra distance of start $\rightarrow$ station $\rightarrow$ end versus the direct route.