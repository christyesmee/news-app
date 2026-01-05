import os
import requests
import smtplib
import traceback
import sys
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

# Balanced Source List (Global/Neutral/Financial)
SOURCES = [
    "reuters.com", "bloomberg.com", "ft.com", "economist.com",  # Financial/Global
    "aljazeera.com", "scmp.com", "thehindu.com",                # Non-Western Perspectives
    "bbc.co.uk", "dw.com", "france24.com",                      # European Public Broadcasters
    "wsj.com", "cnbc.com", "apnews.com", "politico.com"         # US/Political
]

def get_working_model():
    """Auto-selects the best available Gemini model."""
    genai.configure(api_key=GEMINI_API_KEY)
    try:
        # Get all text-generation models available to your key
        all_models = [m.name.replace("models/", "") for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        
        # Priority: Pro (Deep Reasoning) -> Flash (Speed)
        for m in all_models:
            if "1.5-pro" in m: return m
        for m in all_models:
            if "1.5-flash" in m: return m
        
        return all_models[0] if all_models else None
    except Exception as e:
        print(f"!! Model selection error: {e}")
        return None

def fetch_global_news():
    print("--- 1. Fetching Global News ---")
    domains = ",".join(SOURCES)
    # 3-day lookback for freshness
    date_from = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
    
    # URL encoded parameters handled by dictionary to prevent errors
    url = "https://newsapi.org/v2/everything"
    params = {
        'q': '(economy OR politics OR "central bank" OR "supply chain" OR geopolitics OR "trade war" OR "emerging markets")',
        'domains': domains,
        'from': date_from,
        'sortBy': 'publishedAt', # Latest news
        'language': 'en',
        'pageSize': 40, # High volume for better synthesis
        'apiKey': NEWS_API_KEY
    }
    
    try:
        response = requests.get(url, params=params)
        data = response.json()
        articles = data.get("articles", [])
        print(f"-> Found {len(articles)} articles.")
        return articles
    except Exception as e:
        print(f"!! Error fetching news: {e}")
        return []

def analyze_news(articles):
    model_name = get_working_model()
    if not model_name: return None, "No AI models available."

    print(f"--- 2. Analyzing with {model_name} ---")
    
    # Prepare text for AI (Limit to top 25 to fit context window)
    raw_text = ""
    for i, a in enumerate(articles[:25]):
        # Sanitize text
        title = a['title'].replace("{", "(").replace("}", ")")
        source = a['source']['name']
        raw_text += f"[{i+1}] {title} - {source} ({a['publishedAt'][:10]})\n"

    model = genai.GenerativeModel(model_name)
    
    # Disable safety filters to allow political/war analysis
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    prompt = (
        "Role: You are a Strategic Macro-Economic Analyst. Your output is a high-level intelligence brief for an investor focused on global systems, leverage, and capital allocation.\n"
        "Tone: Strictly professional, dense, factual. No conversational filler (no 'Hello', no 'Here is the report').\n"
        "Input: Use the provided news headlines.\n\n"

        "### OUTPUT FORMAT (HTML):\n\n"

        "<h2>EXECUTIVE SUMMARY</h2>\n"
        "<p>Provide a 100-word synthesis of the current global state (Bullish/Bearish/Volatile). Connect the dots between isolated events.</p>\n\n"

        "<h2>1. STRATEGIC DEEP DIVES (The Learning Module)</h2>\n"
        "Select exactly **3** distinct Strategic/Economic Concepts relevant to this week's news (e.g., *Asymmetric Risk*, *Regulatory Capture*, *The Cantillon Effect*, *Thucydides Trap*, *Network Effects*). Do not cite specific books, just the universal concepts.\n"
        "For each, create a <div class='theory-box'>:\n"
        "  <p class='theory-title'>CONCEPT: [NAME OF CONCEPT]</p>\n"
        "  <p><strong>The Logic:</strong> Define the concept technically and logically.</p>\n"
        "  <p><strong>Real-World Application:</strong> Apply this concept to a specific news story from the list (cite the story).</p>\n"
        "  <p><strong>Investment Implication:</strong> How this dynamic creates wealth transfer or risk.</p>\n"
        "</div>\n\n"

        "<h2>2. GLOBAL POLITICAL & MARKET PULSE</h2>\n"
        "Identify the 4 most critical specific developments. For each, use <div class='section'>:\n"
        "  <div class='news-title'>[Headline of Event]</div>\n"
        "  <p><strong>Fact Pattern:</strong> What actually happened? (Cite specific sources like 'Reuters reported...').</p>\n"
        "  <p><strong>Structural Impact:</strong> Why this changes the status quo (e.g., 'Disrupts energy supply chains', 'Alters yield curve expectations').</p>\n"
        "</div>\n\n"

        f"RAW INTEL:\n{raw_text}"
    )

    try:
        response = model.generate_content(prompt, safety_settings=safety_settings)
        if not response.parts:
            return None, "Gemini blocked content (Safety)."
        clean_html = response.text.replace("```html", "").replace("```", "")
        return clean_html, None
    except Exception:
        error_msg = traceback.format_exc()
        return None, error_msg

def create_fallback_html(articles, error_msg):
    html = f"<div style='background:#fee;padding:10px;border:1px solid red;'><h3>⚠️ Analysis Failed</h3><pre>{error_msg}</pre></div><ul>"
    for a in articles[:15]:
        html += f"<li><b>{a['title']}</b><br><i>{a['source']['name']}</i></li>"
    html += "</ul>"
    return html

def send_email(html_content, subject_prefix=""):
    print("--- 3. Sending Email ---")
    
    # Load HTML Template
    try:
        with open('email_template.html', 'r', encoding='utf-8') as f:
            template_str = f.read()
    except FileNotFoundError:
        print("!! Template not found, using simple fallback.")
        template_str = "<html><body>{{CONTENT}}</body></html>"

    final_body = template_str.replace("{{DATE}}", datetime.now().strftime('%B %d, %Y').upper())
    final_body = final_body.replace("{{CONTENT}}", html_content)

    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"{subject_prefix} 🌐 Strategic Intel: {datetime.now().strftime('%Y-%m-%d')}"
    msg.attach(MIMEText(final_body, 'html'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("vv Email sent successfully!")
    except Exception as e:
        print(f"!! Failed to send email: {e}")

if __name__ == "__main__":
    articles = fetch_global_news()
    if not articles:
        send_email("<p>No significant news found today.</p>", "[EMPTY]")
    else:
        analysis_html, error_msg = analyze_news(articles)
        if analysis_html:
            send_email(analysis_html)
        else:
            send_email(create_fallback_html(articles, error_msg), "[DEBUG]")