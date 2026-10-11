# 🌡️ HeatShift

**Don't just check the temperature. Shift the plan.**

HeatShift is a heat-aware planner for outdoor activities. Instead of showing a number on a weather app and leaving you to figure it out, it takes your actual schedule (PE class, delivery shift, football practice, lab setup) and tells you **what to move, when to move it, and why**.

**🔗 Live demo:** [http://3.27.238.131:8501](http://3.27.238.131:8501)
> Hosted on Amazon EC2 (Sydney, `ap-southeast-2`). Plain HTTP, so your browser may say "Not secure".

 **The idea in one line:** weather apps answer "how hot is it?" HeatShift answers "what should I do about it?"

---

## What it does

1. You enter a city and a list of outdoor activities (start time, duration, effort level, and the window each one is allowed to move within).
2. HeatShift pulls the hourly forecast and works out a heat-stress score for every hour, adjusted for how hard the activity is.
3. For each activity it picks one of three outcomes:

| Outcome | Meaning |
|---|---|
| **Keep** | Already within the heat level you're willing to accept |
| **Move** | A cooler slot exists inside the activity's window, so it suggests the best one and explains the drop |
| **Keep + precautions** | No cooler slot fits, so it flags the risk and suggests shortening it or adding shade breaks |

4. Every row also gets a **hydration suggestion**, which ties the project to the *Water* half of the theme.

### The heatwave simulator

Real heatwaves don't schedule themselves around demo day. A sidebar slider adds **0 to +8 °C** to the forecast, so you can turn a mild day into a dangerous one live and watch rows turn red and start moving.

---

## How the heat score works

For each hour, HeatShift starts from the forecast's *apparent temperature* (what it feels like, including humidity) and adds:

| Factor | Adjustment |
|---|---|
| Heatwave simulator | + the slider value |
| Effort: Light / Moderate / Heavy | + 0 / + 2 / + 4 °C |
| UV index 6 to 7 / 8 and above | + 1 / + 2 °C |

The result maps to a level:

| Score | Level |
|---|---|
| below 32 °C | 🟢 Safe |
| 32 to 38 °C | 🟡 Caution |
| 38 to 45 °C | 🟠 Danger |
| above 45 °C | 🔴 Extreme |

For multi-hour activities it uses the **worst hour**, then searches every start time in the activity's window for the lowest peak, breaking ties by staying closest to the original time.

> ⚠️ **Honest note:** these thresholds are a simplified, demo-grade approximation inspired by heat-index categories. They are **not medical advice** and haven't been clinically validated. All the numbers live in a few dictionaries at the top of `app.py` so they're easy to tune.

---

## Architecture

```
Browser
   │
   ▼
Streamlit app (app.py) ── runs on Amazon EC2
   ├── Open-Meteo API ........ hourly forecast (free, no key)
   ├── Heat engine ........... score per hour
   ├── Scheduler ............. keep / move / precautions
   ├── Amazon DynamoDB ....... saved plans
   └── Amazon Bedrock ........ optional plain-English explanations
```

| AWS service | Role in the project |
|---|---|
| **EC2** | Hosts the Streamlit app |
| **IAM role** | Lets the instance call DynamoDB and Bedrock with no keys in the code |
| **DynamoDB** | Stores schedules so "Save plan" and "Load last plan" survive a page refresh |
| **Bedrock** *(optional)* | Rewrites each recommendation as one friendly sentence |

> **Design choice:** all the numbers come from plain Python. Bedrock only rephrases facts the engine already computed, so it can never invent data. If Bedrock is unavailable (quota, permissions, region), the app silently falls back to its built-in explanations and nothing breaks.

---

## Run it locally

You only need Python 3.9+. Everything except Save/Load and Bedrock works without any AWS setup.

```bash
git clone https://github.com/iskushpatel/HeatShift.git
cd HeatShift
pip install -r requirements.txt
streamlit run app.py
```

Then open http://localhost:8501.

**`requirements.txt`**

```
streamlit
requests
pandas
boto3
jinja2
```

---

## Deploy on AWS

1. **DynamoDB:** create a table named `heatshift-plans` with partition key `plan_id` (String) and on-demand capacity.
2. **IAM:** create a role for EC2 with DynamoDB access (and Bedrock access if you want AI explanations), and attach it to the instance.
3. **EC2:** launch an Amazon Linux 2023 instance in the same region as the table, with a security group that allows inbound **TCP 8501** (and 22 for SSH).
4. **On the server:**

```bash
sudo dnf install -y git python3-pip tmux
git clone https://github.com/iskushpatel/HeatShift.git
cd HeatShift
pip3 install -r requirements.txt
tmux new -s app
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

Detach with `Ctrl+B`, then `D`, and open `http://<public-ip>:8501`.

**Updating later:** `git pull`, then restart Streamlit.

> **Deployed instance:** this project is running on a `t3.small` EC2 instance in `ap-southeast-2`, reachable at http://3.27.238.131:8501. The address is the instance's public IP, so it changes if the instance is stopped and started (an Elastic IP would keep it fixed).

---

## Configuration

Everything has a sensible default. Override with environment variables if needed.

| Variable | Default | Purpose |
|---|---|---|
| `AWS_REGION` | `ap-southeast-2` | Region of your DynamoDB table and Bedrock calls |
| `HEATSHIFT_TABLE` | `heatshift-plans` | DynamoDB table name |
| `BEDROCK_MODEL_ID` | *(empty)* | Bedrock model or inference profile ID, or paste it in the sidebar |

---

## Project layout

```
HeatShift/
├── app.py             ← the whole app: heat engine, scheduler, UI, AWS calls
├── requirements.txt
└── README.md
```

One file on purpose. It's small enough to read top to bottom in ten minutes.

---

## Known limitations

- Heat thresholds are approximate and not medically validated.
- Forecast covers three days (Open-Meteo's free hourly data, via `forecast_days=3`).
- Bedrock explanations depend on account quotas, and new AWS accounts often start with very low limits, so the app falls back to built-in text.
- The demo is served over plain HTTP on port 8501.
- Save/Load reads the most recent plan only and isn't per-user. There's no login.

## Where it could go next

- Morning heat alerts via EventBridge and SNS
- Calendar export (`.ics`) so moved activities land straight in your calendar
- Per-user plans with sign-in
- Local sensor or campus weather-station data in place of the regional forecast
- Scoped-down IAM permissions (one table, one model) for production

---

## Credits

Forecasts and geocoding by [Open-Meteo](https://open-meteo.com/). Built with Streamlit, pandas, boto3 and AWS.

---

*The best heat advice isn't a temperature, it's a new start time.*
