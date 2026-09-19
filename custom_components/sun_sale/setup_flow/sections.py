"""Section and page ids of the setup menu tree, with their display labels."""
from __future__ import annotations

from .tariffs import SIDE_LABELS, SIDE_PAGES, SIDES, side_menu

# Root rows that open a section. Each name is also the step that opens it.
SECTION_PRICES = "prices"
SECTION_INVERTER = "inverter"
SECTION_FORECAST = "forecast"

# Root rows that are not sections.
HUB_NAME = "installation_name"
HUB_FINISH = "finish"

# Form pages (step ids). A page counts once its Confirm button was pressed.
PAGE_PRICE_FEED = "price_feed"
PAGE_PRICE_LEVELS = "price_levels"
PAGE_PRICE_FORECAST = "price_forecast"
PAGE_PLATFORM = "inverter_platform"
PAGE_BATTERY = "battery"
PAGE_MAPPING = "inverter_mapping"
PAGE_SOURCES = "sources"
PAGE_BMS = "bms"
PAGE_SOURCES_ENERGY = "sources_energy"

# The price-feed form's follow-up for picking the source and sensor by hand;
# confirming it confirms PAGE_PRICE_FEED.
STEP_PRICE_FEED_MANUAL = "price_feed_manual"
# The sources pages' follow-up for the rows that chose Other; confirming it
# confirms whichever sources page opened it.
STEP_SOURCES_MANUAL = "sources_manual"
# The BMS page's follow-up, confirming the picked battery device's sensors.
STEP_BMS_DETAILS = "bms_details"

# The buy / sell price menus inside the prices section (menus, not pages).
PRICE_SIDE_MENUS = {side_menu(side): side for side in SIDES}

# Every page that belongs to a section; confirming any of them adds the section
# to the installation, and ✕ Remove forgets them all.
SECTION_PAGES = {
        SECTION_PRICES: (PAGE_PRICE_FEED, *SIDE_PAGES, PAGE_PRICE_LEVELS, PAGE_PRICE_FORECAST),
        SECTION_INVERTER: (PAGE_PLATFORM, PAGE_BATTERY, PAGE_MAPPING, PAGE_BMS, PAGE_SOURCES, PAGE_SOURCES_ENERGY),
        SECTION_FORECAST: (SECTION_FORECAST,),
    }

SECTION_LABELS = {
    SECTION_PRICES: "Electricity prices",
    SECTION_INVERTER: "Inverter and battery",
    SECTION_FORECAST: "Solar forecast",
}

PAGE_LABELS = {
    PAGE_PRICE_FEED: "Price source and sensor",
    **{menu: SIDE_LABELS[side] for menu, side in PRICE_SIDE_MENUS.items()},
    **{page: f"{SIDE_LABELS[side]}: {label[0].lower()}{label[1:]}" for page, (side, label) in SIDE_PAGES.items()},
    PAGE_PRICE_LEVELS: "Price level",
    PAGE_PRICE_FORECAST: "Price forecast weather",
    PAGE_PLATFORM: "Inverter and power ratings",
    PAGE_BATTERY: "Battery",
    PAGE_MAPPING: "Entity mapping",
    PAGE_BMS: "Dedicated battery BMS",
    PAGE_SOURCES: "Inverter power sensors",
    PAGE_SOURCES_ENERGY: "Energy counters",
}

# What an optional page falls back to while it has not been confirmed.
OPTIONAL_PAGE_HINTS = {
    PAGE_PRICE_FEED: "not needed while both prices are fixed",
    PAGE_PRICE_LEVELS: "defaults apply",
    PAGE_PRICE_FORECAST: "weather auto-detected",
    PAGE_BMS: "battery read from the inverter",
    PAGE_SOURCES: "none set",
    PAGE_SOURCES_ENERGY: "none set",
}

# The window a form's "✕ discard and go back" returns to, keyed by its step id.
# The names match the menu titles the user already saw; "hub" is the config
# flow's root, "init" the options flow's.
BACK_NAMES = {
    SECTION_PRICES: SECTION_LABELS[SECTION_PRICES],
    SECTION_INVERTER: SECTION_LABELS[SECTION_INVERTER],
    **{menu: SIDE_LABELS[side] for menu, side in PRICE_SIDE_MENUS.items()},
    "hub": "setup",
    "init": "settings",
}
