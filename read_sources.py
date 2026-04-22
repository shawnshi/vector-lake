import os

files = [
    r"C:\Users\shich\.gemini\MEMORY\raw\DigitalHealthWeeklyBrief\DHWB-20260413.md",
    r"C:\Users\shich\.gemini\MEMORY\raw\DigitalHealthWeeklyBrief\DHWB-20260419.md",
    r"C:\Users\shich\.gemini\MEMORY\raw\HealthcareIndustryRadar\DHWB-Radar-20260302.md",
    r"C:\Users\shich\.gemini\MEMORY\raw\HealthcareIndustryRadar\DHWB-Radar-20260308.md",
    r"C:\Users\shich\.gemini\MEMORY\raw\HealthcareIndustryRadar\DHWB-Radar-20260315.md"
]

for f in files:
    print(f"=== FILE: {os.path.basename(f)} ===")
    if os.path.exists(f):
        with open(f, "r", encoding="utf-8") as s:
            print(s.read())
    else:
        print("FILE_NOT_FOUND")
    print("=== END ===")

