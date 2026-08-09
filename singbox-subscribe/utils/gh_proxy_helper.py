import re

def set_gh_proxy(config, selected_index=0):


    # Список сервисов ускорения (название, префикс)
    proxy_methods = [
        ("gh-proxy.com", "https://gh-proxy.com/"),
        ("gh.sageer.me", "https://gh.sageer.me/"),
        ("ghproxy.com", "https://ghproxy.com/"),
        ("mirror.ghproxy.com", "https://mirror.ghproxy.com/"),
        ("jsDelivr", "jsdelivr"),
        ("jsDelivr CF", "testingcf.jsdelivr.net")
    ]

    # Все префиксы HTTP-прокси
    #all_prefixes = [prefix for _, prefix in proxy_methods if not prefix.startswith("jsdelivr") and not prefix.startswith("testingcf")]
    selected_name, selected_prefix = proxy_methods[selected_index]
    all_prefixes = [prefix for _, prefix in proxy_methods]

    def restore_raw_url(line):
        # Определяем jsDelivr или CF-образ, возвращаем обратно в raw.githubusercontent.com
        jsdelivr_pattern = r'https://(?:cdn\.jsdelivr\.net|testingcf\.jsdelivr\.net)/gh/([^/]+)/([^@]+)@([^/]+)/(.*)'
        match = re.match(jsdelivr_pattern, line)
        if match:
            
            user, repo, branch, path = match.groups()
            return f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"

        # Определяем другие префиксы ускорения
        for prefix in all_prefixes:
            if line.startswith(prefix):
                if selected_prefix in ("jsdelivr", "testingcf.jsdelivr.net") and "raw.githubusercontent.com" not in line:
                    return line
                return line.replace(prefix, selected_prefix, 1)
        return line

    def convert_to_jsdelivr(raw_url, domain="cdn.jsdelivr.net"):
        match = re.match(r'https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.*)', raw_url)
        if match:
            user, repo, branch, path = match.groups()
            return f"https://{domain}/gh/{user}/{repo}@{branch}/{path}"
        return raw_url

    def apply_proxy(line):
        original = restore_raw_url(line)

        if selected_prefix in ("jsdelivr", "testingcf.jsdelivr.net"):
            # Проверяем, является ли это raw-форматом
            if "raw.githubusercontent.com" not in original:
                # print(f"⚠️  Невозможно использовать ускорение jsDelivr для не-raw.github ссылок, сохранена исходная ссылка:\n  {original}")
                return original
            domain = "cdn.jsdelivr.net" if selected_prefix == "jsdelivr" else "testingcf.jsdelivr.net"
            return convert_to_jsdelivr(original, domain=domain)
        else:
            return re.sub(
                r'^https://raw\.githubusercontent\.com/',
                selected_prefix + 'raw.githubusercontent.com/',
                original
            )

    if isinstance(config, str):
        return apply_proxy(config)
    elif isinstance(config, list):
        return [apply_proxy(line) for line in config]
    else:
        raise TypeError("config должен быть строкой или списком строк")