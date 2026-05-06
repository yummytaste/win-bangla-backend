def is_bad(url):
    url = url.lower()

    bad_words = [
        "logo",
        "banner",
        "youtube",
        "telegram",
        "whatsapp",
        "facebook",
        "ads",
        "advertisement",
        "cs101",
        "lottery-sambad.png",
        "install",
        "app",
        "playstore",
        "icon",
        "favicon",
    ]

    return any(x in url for x in bad_words)
