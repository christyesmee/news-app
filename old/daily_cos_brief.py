import os
import requests
import smtplib
import traceback
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai
from datetime import datetime, timedelta

# --- CONFIGURATION ---
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

# --- STRATEGIC SOURCES ---
SOURCES = [
    # Strategic/Management
    "economist.com", "hbr.org", "mckinsey.com", 
    # Tech Analysis
    "semianalysis.com", "theregister.com", "techcrunch.com", "anandtech.com",
    # Global/European News
    "reuters.com", "bloomberg.com", "ft.com", "politico.eu", "euronews.com",
    "scmp.com", "nikkei.com", "caixinglobal.com", "digitimes.com"
]

def get_working_model():
    genai.configure(api_key=GEMINI_API_KEY)
    try:
        all_models = [m.name.replace("models/", "") for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        for m in all_models:
            if "1.5-pro" in m: return m
        for m in all_models:
            if "1.5-flash" in m: return m
        return all_models[0] if all_models else None
    except Exception as e:
        print(f"!! Model selection error: {e}")
        return None

def fetch_strategic_news():
    print("--- 1. Fetching Chief of Staff Intel ---")
    domains = ",".join(SOURCES)
    date_from = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    
    # LOGIC FIX: We group ALL topics together, AND then apply the region filter.
    # This ensures we only get "NVIDIA" news IF it is relevant to "Europe/Global Context".
    query = (
        '('
        # Topic Group 1: Strategic/Policy
        'quantum OR virtualization OR "EuroHPC" OR "digital sovereignty" OR "high performance computing" OR "AI regulation" OR "strategic autonomy" '
        'OR '
        # Topic Group 2: Tech/Supply Chain
        'semiconductor OR "AI chips" OR GPU OR "data center" OR foundry OR "supply chain" OR "rare earth" OR lithography OR ASML OR TSMC OR NVIDIA'
        ') '
        'AND '
        # Context Group: Must be relevant to these regions
        '(Europe OR EU OR Germany OR UK OR France OR Netherlands OR "North West Europe" OR China OR US)'
    )
    
    url = "https://newsapi.org/v2/everything"
    params = {
        'q': query,
        'domains': domains,
        'from': date_from,
        'sortBy': 'relevance', 
        'language': 'en',
        'pageSize': 40,
        'apiKey': NEWS_API_KEY
    }
    
    try:
        response = requests.get(url, params=params)
        data = response.json()
        articles = data.get("articles", [])
        print(f"-> Found {len(articles)} strategic articles.")
        return articles
    except Exception as e:
        print(f"!! Error fetching news: {e}")
        return []

def analyze_news(articles):
    model_name = get_working_model()
    if not model_name: return None, "No AI models available."

    print(f"--- 2. Analyzing with {model_name} ---")
    
    raw_text = ""
    for i, a in enumerate(articles[:30]):
        safe_title = a['title'].replace('"', "'")
        img_url = a['urlToImage'] if a['urlToImage'] else "NO_IMAGE"
        raw_text += f"ID: {i+1} | Title: {safe_title} | Source: {a['source']['name']} | URL: {a['url']} | IMG: {img_url}\n"

    model = genai.GenerativeModel(model_name)
    
    # SAFETY SETTINGS (CRITICAL):
    # This prevents the AI from blocking "Political/War" news which is essential for this brief.
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    ]

    prompt = (
        "Role: You are the Chief of Staff to the SVP of North West Europe. Your boss needs a strategic briefing, not just news.\n"
        "Input: News from the last 7 days covering: Semiconductors/Compute, Data Centers, Quantum, Virtualization, and EU Tech Policy.\n"
        "Goal: Synthesize this into a 'Decision Advantage' brief. Connect the dots between the Tech Market (e.g. Chip shortage) and Policy (e.g. EuroHPC) also, the dots between policy (EuroHPC) and business (Hardware sales: Virtualization/HPC/Compute/storage/Quantum).\n"
        "Tone: Executive, sophisticated, forward-looking. Use 'We' perspective for the European market.\n\n"
        
        "### INSTRUCTIONS:\n"
        "1. Select the BEST image from the articles. Insert it at the top of the relevant section using: <img src='IMG_URL' class='story-image'>\n"
        "2. Cite sources using clickable footnotes: <a href='URL'>[1]</a>.\n"
        "3. Focus on: UK, Netherlands, Germany, France, Nordics.\n\n"

        "### OUTPUT FORMAT (HTML):\n"
        "<h2>🇪🇺 Strategic Horizon: NW Europe</h2>\n"
        "<p><strong>Executive Summary:</strong> (2-3 sentences on the macro-strategic vibe for the SVP).</p>\n\n"

        "\n"
        "<div class='section'>\n"
        "  \n"
        "  <div class='news-title'>HEADLINE (e.g., 'EuroHPC Expansion')</div>\n"
        "  <p><strong>The Situation:</strong> What is happening? <a href='URL'>[1]</a></p>\n"
        "  <p><strong>Strategic Implication:</strong> Why does this matter for Europe? (e.g. 'Impacts our German data center strategy' or 'New funding available in Netherlands').</p>\n"
        "  <p><strong>Action/Thought:</strong> A 'Chief of Staff' recommendation (e.g. 'Monitor regulatory shift' or 'Potential partnership opportunity').</p>\n"
        "</div>\n\n"

        f"RAW INTEL:\n{raw_text}"
    )

    try:
        response = model.generate_content(prompt, safety_settings=safety_settings)
        return response.text.replace("```html", "").replace("```", ""), None
    except Exception:
        error_msg = traceback.format_exc()
        return None, error_msg

def send_email(html_content):
    print("--- 3. Sending Email ---")
    
    # 1. Prepare HTML
    try:
        with open('email_template.html', 'r', encoding='utf-8') as f:
            template_str = f.read()
    except FileNotFoundError:
        template_str = "<html><body>{{CONTENT}}</body></html>"

    # Custom Header for CoS
    final_html = template_str.replace("Compute Market Intel", "CoS Strategic Brief")
    final_html = final_html.replace("⚡", "🇪🇺") 
    final_html = final_html.replace("{{DATE}}", datetime.now().strftime('%B %d, %Y'))
    final_html = final_html.replace("{{CONTENT}}", html_content)

    # 2. Plain Text (Voice)
    def clean_html(raw_html):
        cleanr = re.compile('<.*?>')
        cleantext = re.sub(cleanr, '', raw_html)
        return cleantext.replace("&nbsp;", " ").replace("  ", " ").strip()
    
    plain_text = f"Good morning. Here is your CoS Strategic Brief for {datetime.now().strftime('%A, %B %d')}.\n\n"
    plain_text += clean_html(html_content)

    msg = MIMEMultipart('alternative')
    msg['From'] = EMAIL_USER
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"EU CoS Briefing: {datetime.now().strftime('%Y-%m-%d')}"

    msg.attach(MIMEText(plain_text, 'plain'))
    msg.attach(MIMEText(final_html, 'html'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("vv CoS Email sent!")
    except Exception as e:
        print(f"!! Failed to send email: {e}")

if __name__ == "__main__":
    articles = fetch_strategic_news()
    if articles:
        html, err = analyze_news(articles)
        if html: send_email(html)
    else:
        print("No strategic news found.")