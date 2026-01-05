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

# 10 Diverse International Sources
SOURCES = [
    "reuters.com", "aljazeera.com", "bbc.co.uk", "scmp.com", 
    "dw.com", "bloomberg.com", "wsj.com", "theguardian.com", 
    "france24.com", "thehindu.com"
]

def get_working_model():
    """
    Scans your account for ANY working text model and returns the best one.
    """
    print("--- Selecting Best Available AI Model ---")
    genai.configure(api_key=GEMINI_API_KEY)
    
    try:
        # 1. Get all models that support text generation
        all_models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                name = m.name.replace("models/", "")
                all_models.append(name)
        
        print(f"-> Your Account has access to: {all_models}")

        if not all_models:
            print("!! FATAL: No models found with 'generateContent' capability.")
            return None

        # 2. Try to find a 'Pro' or 'Flash' model first (Best quality)
        for m in all_models:
            if "1.5-pro" in m: return m
        for m in all_models:
            if "1.5-flash" in m: return m
            
        # 3. If no specific preference found, JUST TAKE THE FIRST ONE
        # This prevents the 404 error by never guessing a name that doesn't exist.
        print(f"-> No preferred model found. Defaulting to first available: {all_models[0]}")
        return all_models[0]

    except Exception as e:
        print(f"!! Error listing models: {e}")
        return None

def fetch_global_news():
    print("--- 1. Fetching World News ---")
    domains = ",".join(SOURCES)
    date_from = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
    
    url = (
        f"https://newsapi.org/v2/everything?"
        f"q=(economy OR politics OR diplomacy OR 'international relations')&"
        f"domains={domains}&"
        f"from={date_from}&"
        f"sortBy=publishedAt&"
        f"language=en&"
        f"pageSize=20&"
        f"apiKey={NEWS_API_KEY}"
    )
    
    try:
        response = requests.get(url)
        data = response.json()
        articles = data.get("articles", [])
        print(f"-> Found {len(articles)} articles.")
        return articles
    except Exception as e:
        print(f"!! Error fetching news: {e}")
        return []

def analyze_news(articles):
    # DYNAMIC SELECTION
    model_name = get_working_model()
    
    if not model_name:
        return None, "Could not find any available Gemini models for this API Key."

    print(f"--- 2. Analyzing with {model_name} ---")
    
    raw_text = ""
    for i, a in enumerate(articles[:15]):
        raw_text += f"{i+1}. {a['title']} - Source: {a['source']['name']}\n"

    model = genai.GenerativeModel(model_name)
    
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    prompt = (
        "You are an expert Professor of International Relations. "
        "Review these news headlines. Create a HTML Daily Intelligence Briefing.\n"
        "INSTRUCTIONS:\n"
        "1. Identify top 3 global themes.\n"
        "2. For each theme: Headline, 2-sentence summary, and a <div class='theory-box'> explaining the political theory concept.\n"
        f"HEADLINES:\n{raw_text}"
    )

    try:
        response = model.generate_content(prompt, safety_settings=safety_settings)
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