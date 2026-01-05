import os
import requests
import smtplib
import traceback
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

# 10 Diverse International Sources (Balanced Worldview)
SOURCES = [
    "reuters.com",       # Global/Neutral
    "aljazeera.com",     # Middle East/Global South
    "bbc.co.uk",         # UK/Public Service
    "scmp.com",          # Asia/China Perspective
    "dw.com",            # Europe/Germany
    "bloomberg.com",     # Economics Focus
    "wsj.com",           # US Business/Conservative
    "theguardian.com",   # UK/Progressive
    "france24.com",      # France/Global
    "thehindu.com"       # India/South Asia
]

def get_working_model():
    """Auto-selects the best available Gemini model to prevent 404 errors."""
    print("--- Selecting Best Available AI Model ---")
    genai.configure(api_key=GEMINI_API_KEY)
    try:
        my_models = [m.name.replace("models/", "") for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        
        # Preference order: Flash is faster/cheaper, Pro is smarter. 
        # Since we want deep theory, we try Pro first, but fall back to Flash if needed.
        preferences = [
            "gemini-1.5-pro", 
            "gemini-1.5-pro-latest",
            "gemini-1.5-pro-001",
            "gemini-1.5-flash", 
            "gemini-1.5-flash-latest"
        ]
        
        for pref in preferences:
            if pref in my_models:
                print(f"vv SELECTED: {pref}")
                return pref
        
        return "gemini-pro" # Ultimate fallback
    except Exception as e:
        print(f"!! Model selection error: {e}. Defaulting to gemini-1.5-flash")
        return "gemini-1.5-flash"

def fetch_global_news():
    print("--- 1. Fetching World News (Politics & Econ) ---")
    
    # Construct the advanced query
    domains = ",".join(SOURCES)
    # We look back 3 days to ensure we get the 'Latest' breaking news, but enough volume
    date_from = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
    
    url = (
        f"https://newsapi.org/v2/everything?"
        f"q=(economy OR politics OR diplomacy OR 'international relations')&"
        f"domains={domains}&"
        f"from={date_from}&"
        f"sortBy=publishedAt&" # Latest news first
        f"language=en&"
        f"pageSize=20&" # Fetch 20 to give AI enough context
        f"apiKey={NEWS_API_KEY}"
    )
    
    try:
        response = requests.get(url)
        data = response.json()
        articles = data.get("articles", [])
        print(f"-> Found {len(articles)} articles from diverse sources.")
        return articles
    except Exception as e:
        print(f"!! Error fetching news: {e}")
        return []

def analyze_news(articles):
    model_name = get_working_model()
    print(f"--- 2. Analyzing with {model_name} ---")
    
    # Prepare text for AI
    raw_text = ""
    for i, a in enumerate(articles[:15]): # Limit to 15 to fit context window
        raw_text += f"{i+1}. {a['title']} - Source: {a['source']['name']} - Date: {a['publishedAt'][:10]}\n"

    model = genai.GenerativeModel(model_name)

    # Safety: Allow political content
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    prompt = (
        "You are an expert Professor of International Relations and Economics. "
        "Review these latest news headlines from diverse global sources. "
        "Create a HTML Daily Intelligence Briefing.\n\n"
        
        "INSTRUCTIONS:\n"
        "1. Identify the top 3-4 most critical global themes (e.g., 'Trade Wars', 'Elections', 'Energy Crisis').\n"
        "2. For each theme, create a <div class='section'> block.\n"
        "3. Inside the block:\n"
        "   - Add a headline <div class='news-title'>Theme Title</div>\n"
        "   - Summarize the development in 2 sentences. Cite the specific sources used (e.g. 'reported by Al Jazeera and WSJ').\n"
        "   - CRITICAL STEP: Add a <div class='theory-box'>.\n"
        "     Inside it, define the political/economic THEORY or concept at play.\n"
        "     (e.g. explain 'Comparative Advantage', 'Soft Power', 'Realism', 'Inflationary Spiral').\n"
        "     Label it <span class='theory-title'>CONCEPT: [Name of Theory]</span>.\n\n"
        
        f"NEWS DATA:\n{raw_text}"
    )

    try:
        response = model.generate_content(prompt, safety_settings=safety_settings)
        if not response.parts:
            return None, "Gemini returned empty response (Safety Block)."
        
        clean_html = response.text.replace("```html", "").replace("```", "")
        return clean_html, None
    except Exception:
        error_msg = traceback.format_exc()
        return None, error_msg

def create_fallback_html(articles, error_msg):
    html = f"<div style='background:#fee;padding:10px;'><h3>⚠️ Analysis Failed</h3><pre>{error_msg}</pre></div><ul>"
    for a in articles:
        html += f"<li><b>{a['title']}</b><br><i>{a['source']['name']}</i></li>"
    html += "</ul>"
    return html

def send_email(html_content, subject_prefix=""):
    print("--- 3. Sending Email ---")
    try:
        with open('email_template.html', 'r') as f:
            template_str = f.read()
    except:
        template_str = "<html><body><h1>Global Brief</h1>{{CONTENT}}</body></html>"

    final_body = template_str.replace("{{DATE}}", datetime.now().strftime('%A, %B %d, %Y'))
    final_body = final_body.replace("{{CONTENT}}", html_content)

    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"{subject_prefix} 🌍 Global Intelligence: {datetime.now().strftime('%Y-%m-%d')}"
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
        send_email("<p>No articles found matching the criteria today.</p>", "[EMPTY]")
    else:
        analysis_html, error_msg = analyze_news(articles)
        if analysis_html:
            send_email(analysis_html)
        else:
            send_email(create_fallback_html(articles, error_msg), "[DEBUG]")