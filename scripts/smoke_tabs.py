import sys, warnings
warnings.filterwarnings("ignore")
from streamlit.testing.v1 import AppTest
sections = sys.argv[1:] or ["Curve","Sizing","Path","Releases","Vote count","Macro","Labour","Inflation","Data"]
for sec in sections:
    at = AppTest.from_file("C:/Users/User/Marco/Australia-Rates/app.py", default_timeout=240)
    at.query_params["section"] = sec
    at.run()
    errs = [e.value for e in at.exception]
    print(f"== {sec}: {'OK' if not errs else 'ERR'}")
    for e in errs:
        print("   ", str(e)[:2000])
