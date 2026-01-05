import os
import requests
import smtplib
import traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai
from datetime import datetime

# --- CONFIGURATION ---
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

def get_working_model():
    """
    Asks Google API what models are available to THIS specific API Key
    and selects the best one automatically.
    """
    print("--- Selecting Best Available AI Model ---")
    genai.configure(api_key=GEMINI_API_KEY)
    
    try:
        # Get list of all models your key can access
        my_models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                # Strip the "models/" prefix if it exists
                clean_name = m.name.replace("models/", "")
                my_models.append(clean_name)
        
        print(f"-> Available Models: {my_models}")

        # Preference list: Try these in order
        preferences = [
            "gemini-1.5-flash", 
            "gemini-1.5-flash-001",
            "gemini-1.5-flash-latest",
            "gemini-1.5-pro",
            "gemini-1.5-pro-001",
            "gemini-pro"
        ]

        # Find the first match
        for pref in preferences:
            if pref in my_models:
                print(f"vv SELECTED: {pref}")
                return pref
        
        # If no exact match, grab the first one that has "gemini" in the name
        fallback = next((m for m in my_models if "gemini" in m), "gemini-pro")
        print(f"-> Fallback Selection: {fallback}")
        return fallback

    except Exception as e:
        print(f"!! Error listing models: {e}")
        print("-> Defaulting to 'gemini-pro'")
        return "gemini-pro"

def fetch_global_news():
    print("--- 1. Fetching News ---")
    # Broad query to ensure we get results
    url = (
        f"https://newsapi.org/v2/top-headlines?"
        f"category=general&"
        f"language=en&"
        f"pageSize=10&"
        f"apiKey={NEWS_API_KEY}"
    )
    
    try:
        response = requests.get(url)
        data = response.json()
        articles = data.get("articles", [])
        return articles
    except Exception as e:
        print(f"!! Error fetching news: {e}")
        return []

def analyze_news(articles):
    # DYNAMICALLY GET THE MODEL NAME
    model_name = get_working_model()
    
    print(f"--- 2. Analyzing with {model_name} ---")
    
    raw_text = ""
    for i, a in enumerate(articles):
        raw_text += f"{i+1}. {a['title']} (Source: {a['source']['name']})\n"

    model = genai.GenerativeModel(model_name)

    # Safety settings to preventing blocking
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    prompt = (
        "Summarize these news headlines into a daily briefing HTML format. "
        "Use <div class='section'> for each topic. "
        "Include a <div class='theory-box'> explaining the context.\n\n"
        f"HEADLINES:\n{raw_text}"
    )

    try:
        response = model.generate_content(prompt, safety_settings=safety_settings)
        
        if not response.parts:
            return None, "Gemini returned empty response (Safety Block)."
            
        clean_html = response.text.replace("```html", "").replace("```", "")
        return clean_html, None
        
    except Exception:
        error_msg = traceback.format_exc()
        print(f"!! Gemini Error Trace:\n{error_msg}")
        return None, error_msg

def create_fallback_html(articles, error_msg):
    html = f"""
    <div style='background-color: #fee; border: 1px solid red; padding: 10px; margin-bottom: 20px;'>
        <h3>⚠️ AI Generation Failed</h3>
        <b>Error Details:</b><br>
        <pre style='white-space: pre-wrap; font-size: 10px;'>{error_msg}</pre>
    </div>
    <h3>Raw Headlines</h3>
    <ul>
    """
    for a in articles:
        html += f"<li><b>{a['title']}</b><br><i>{a['source']['name']}</i></li><br>"
    html += "</ul>"
    return html

def send_email(html_content, subject_prefix=""):
    print("--- 3. Sending Email ---")
    try:
        with open('email_template.html', 'r') as f:
            template_str = f.read()
    except:
        template_str = "<html><body><h1>Daily Brief</h1>{{CONTENT}}</body></html>"

    final_body = template_str.replace("{{DATE}}", datetime.now().strftime('%Y-%m-%d'))
    final_body = final_body.replace("{{CONTENT}}", html_content)

    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"{subject_prefix} 🌍 World Brief: {datetime.now().strftime('%Y-%m-%d')}"
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
        send_email("<p>Error: NewsAPI returned 0 articles.</p>", "[ERROR]")
    else:
        analysis_html, error_msg = analyze_news(articles)
        if analysis_html:
            send_email(analysis_html)
        else:
            fallback = create_fallback_html(articles, error_msg)
            send_email(fallback, "[DEBUG]")