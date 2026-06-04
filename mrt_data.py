# mrt_data.py

# 1. Visual formatting map
LINE_FORMAT = {
    "EWL": "🟢 East West Line",
    "NSL": "🔴 North South Line",
    "NEL": "🟣 North East Line",
    "CCL": "🟡 Circle Line",
    "DTL": "🔵 Downtown Line",
    "TEL": "🟤 Thomson-East Coast Line"
}

# 2. Master Map: Every station network (Used for standard stations)
MRT_LINES = {
    "EWL": ["PASIR RIS", "TAMPINES", "SIMEI", "TANAH MERAH", "BEDOK", "KEMBANGAN", "EUNOS", "PAYA LEBAR", "ALJUNIED", "KALLANG", "LAVENDER", "BUGIS", "CITY HALL", "RAFFLES PLACE", "TANJONG PAGAR", "OUTRAM PARK", "TIONG BAHRU", "REDHILL", "QUEENSTOWN", "COMMONWEALTH", "BUONA VISTA", "DOVER", "CLEMENTI", "JURONG EAST", "CHINESE GARDEN", "LAKESIDE", "BOON LAY", "PIONEER", "JOO KOON", "GUL CIRCLE", "TUAS CRESCENT", "TUAS WEST ROAD", "TUAS LINK", "EXPO", "CHANGI AIRPORT"],
    "NSL": ["JURONG EAST", "BUKIT BATOK", "BUKIT GOMBAK", "CHOA CHU KANG", "YEW TEE", "KRANJI", "MARSILING", "WOODLANDS", "ADMIRALTY", "SEMBAWANG", "CANBERRA", "YISHUN", "KHATIB", "YIO CHU KANG", "ANG MO KIO", "BISHAN", "BRADDELL", "TOA PAYOH", "NOVENA", "NEWTON", "ORCHARD", "SOMERSET", "DHOBY GAUT", "CITY HALL", "RAFFLES PLACE", "MARINA BAY", "MARINA SOUTH PIER"],
    "NEL": ["HARBOURFRONT", "OUTRAM PARK", "CHINATOWN", "CLARKE QUAY", "DHOBY GAUT", "LITTLE INDIA", "FARRER PARK", "BOON KENG", "POTONG PASIR", "WOODLEIGH", "SERANGOON", "KOVAN", "HOUGANG", "BUANGKOK", "SENGKANG", "PUNGGOL"],
    "CCL": ["DHOBY GAUT", "BRAS BASAH", "ESPLANADE", "PROMENADE", "NICOLL HIGHWAY", "STADIUM", "MOUNTBATTEN", "DAKOTA", "PAYA LEBAR", "MACPHERSON", "TAI SENG", "BARTLEY", "SERANGOON", "LORONG CHUAN", "BISHAN", "MARYMOUNT", "CALDECOTT", "BOTANIC GARDENS", "FARRER ROAD", "HOLLAND VILLAGE", "BUONA VISTA", "ONE-NORTH", "KENT RIDGE", "HAW PAR VILLA", "PASIR PANJANG", "LABRADOR PARK", "TELOK BLANGAH", "HARBOURFRONT", "BAYFRONT", "MARINA BAY"],
    "DTL": ["BUKIT PANJANG", "CASHEW", "HILLVIEW", "BEAUTY WORLD", "KING ALBERT PARK", "SIXTH AVENUE", "TAN KAH KEE", "BOTANIC GARDENS", "STEVENS", "NEWTON", "LITTLE INDIA", "ROCHOR", "BUGIS", "PROMENADE", "BAYFRONT", "DOWNTOWN", "TELOK AYER", "CHINATOWN", "FORT CANNING", "BENCOOLEN", "JALAN BESAR", "BENDEMEER", "GEYLANG BAHRU", "MATTAR", "MACPHERSON", "UBI", "KAKI BUKIT", "BEDOK NORTH", "BEDOK RESERVOIR", "TAMPINES WEST", "TAMPINES", "TAMPINES EAST", "UPPER CHANGI", "EXPO"],
    "TEL": ["WOODLANDS NORTH", "WOODLANDS", "WOODLANDS SOUTH", "SPRINGLEAF", "LENTOR", "MAYFLOWER", "BRIGHT HILL", "UPPER THOMSON", "CALDECOTT", "STEVENS", "NAPIER", "ORCHARD BOULEVARD", "ORCHARD", "GREAT WORLD", "HAVELOCK", "OUTRAM PARK", "MAXWELL", "SHENTON WAY", "MARINA BAY", "GARDENS BY THE BAY", "TANJONG RHU", "KATONG PARK", "TANJONG KATONG", "MARINE PARADE", "MARINE TERRACE", "SIGLAP", "BAYSIDE", "BEDOK SOUTH", "SUNGEI BEDOK"]
}

# 3. The Intercept Map: Only for complex interchanges
INTERCHANGE_EXITS = {
    "SERANGOON": {
        "EXIT A": ["NEL"], "EXIT B": ["NEL"], "EXIT C": ["NEL"], "EXIT D": ["NEL"],
        "EXIT E": ["CCL"], "EXIT F": ["CCL"], "EXIT G": ["CCL"], "EXIT H": ["CCL"],
        "EXIT E/G": ["CCL"] # Specific OneMap edge case handling
    },
    "DHOBY GAUT": {
        "EXIT A": ["NSL"], "EXIT B": ["NSL"],
        "EXIT C": ["NEL"], "EXIT D": ["NEL"], "EXIT E": ["NEL"],
        "EXIT F": ["CCL"], "EXIT G": ["CCL"]
    },
    "PAYA LEBAR": {
        "EXIT A": ["EWL"], "EXIT B": ["EWL"], "EXIT C": ["EWL"], "EXIT D": ["EWL"],
        "EXIT E": ["CCL"], "EXIT F": ["CCL"]
    },
    "BISHAN": {
        "EXIT A": ["NSL"], "EXIT B": ["NSL"], "EXIT C": ["NSL"], "EXIT D": ["NSL"],
        "EXIT E": ["CCL"]
    },
    "BUONA VISTA": {
        "EXIT A": ["EWL"], "EXIT B": ["EWL"], "EXIT C": ["EWL"],
        "EXIT D": ["CCL"]
    },
    "BOTANIC GARDENS": {
        "EXIT A": ["CCL"], 
        "EXIT B": ["DTL"]
    },
    "NEWTON": {
        "EXIT A": ["NSL"], "EXIT B": ["NSL"],
        "EXIT C": ["DTL"]
    },
    "MACPHERSON": {
        "EXIT A": ["CCL"], 
        "EXIT B": ["DTL"], "EXIT C": ["DTL"]
    },
    "BUGIS": {
        "EXIT A": ["EWL"], "EXIT B": ["EWL"], "EXIT C": ["EWL"],
        "EXIT D": ["DTL"], "EXIT E": ["DTL"], "EXIT F": ["DTL"]
    },
    "TAMPINES": {
        "EXIT A": ["EWL"], "EXIT B": ["EWL"], "EXIT C": ["EWL"],
        "EXIT D": ["DTL"], "EXIT E": ["DTL"], "EXIT F": ["DTL"], "EXIT G": ["DTL"]
    },
    "CHINATOWN": {
        "EXIT A": ["NEL"], "EXIT C": ["NEL"], "EXIT D": ["NEL"], "EXIT E": ["NEL"],
        "EXIT F": ["DTL"], "EXIT G": ["DTL"]
    },
    "LITTLE INDIA": {
        "EXIT A": ["NEL"], "EXIT B": ["NEL"], "EXIT C": ["NEL"], "EXIT D": ["NEL"], "EXIT E": ["NEL"],
        "EXIT F": ["DTL"]
    },
    "EXPO": {
        "EXIT A": ["EWL"], "EXIT B": ["EWL"], "EXIT C": ["EWL"], "EXIT D": ["EWL"],
        "EXIT E": ["DTL"], "EXIT F": ["DTL"], "EXIT G": ["DTL"]
    },
    "OUTRAM PARK": {
        # Using the modernized numbered exits
        "EXIT 1": ["EWL"], "EXIT 2": ["EWL"], "EXIT 3": ["EWL"],
        "EXIT 4": ["NEL"], "EXIT 5": ["NEL"], "EXIT 6": ["NEL"],
        "EXIT 7": ["TEL"], "EXIT 8": ["TEL"]
    },
    "WOODLANDS": {
        "EXIT 1": ["NSL"], "EXIT 2": ["NSL"], "EXIT 3": ["NSL"],
        "EXIT 4": ["TEL"], "EXIT 5": ["TEL"], "EXIT 6": ["TEL"], "EXIT 7": ["TEL"]
    },
    "MARINA BAY": {
        "EXIT 1": ["NSL", "CCL"], "EXIT 2": ["NSL", "CCL"], 
        "EXIT 3": ["TEL"], "EXIT 4": ["TEL"]
    },
    "STEVENS": {
        "EXIT 1": ["DTL"], "EXIT 2": ["DTL"],
        "EXIT 3": ["TEL"], "EXIT 4": ["TEL"]
    },
    "CALDECOTT": {
        "EXIT 1": ["CCL"], 
        "EXIT 2": ["TEL"], "EXIT 3": ["TEL"], "EXIT 4": ["TEL"]
    }
}

def get_line_for_exit(raw_onemap_name: str) -> str:
    """
    Parses the station name. If it's a known interchange, it strictly maps the exit.
    Otherwise, it broadly maps the station to all its active lines.
    """
    upper_name = raw_onemap_name.upper().strip()
    
    # Defaults
    base_station = upper_name.replace(" MRT", "")
    exit_key = None
    
    # 1. Parse out the base station and the exact exit
    # E.g., "SERANGOON MRT (EXIT E/G)" -> base: "SERANGOON", exit: "EXIT E/G"
    if "(" in upper_name:
        parts = upper_name.split("(")
        base_station = parts[0].replace(" MRT", "").strip()
        exit_key = parts[1].replace(")", "").strip()

    line_codes = []

    # 2. THE INTERCEPT: Is this a complex interchange?
    if base_station in INTERCHANGE_EXITS and exit_key:
        # Strict matching: Only get the line for this specific exit
        if exit_key in INTERCHANGE_EXITS[base_station]:
            line_codes = INTERCHANGE_EXITS[base_station][exit_key]
    
    # 3. THE FALLBACK: Standard station or exit wasn't found in interchange map
    if not line_codes:
        for code, stations in MRT_LINES.items():
            if base_station in stations:
                line_codes.append(code)

    # 4. Format for display
    formatted_lines = [LINE_FORMAT[code] for code in line_codes if code in LINE_FORMAT]
    
    if formatted_lines:
        return f" [{', '.join(formatted_lines)}]"
    return ""