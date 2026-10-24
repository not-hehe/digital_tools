// Тег атрибуции: источник последнего значимого захода в пяти куках на весь домен сайта.

(function () {
  // Платёжные, 3DS и SSO-домены: возврат с них - не новый источник. Сравниваются с доменом
  // реферера без поддоменов, поэтому вносить домен второго уровня (bank.example), без масок.
  const PAYMENT_HOSTS = [];
  const CONFIG = {
    // Точка в начале делает куку общей для домена и всех его поддоменов.
    cookieDomain: '.example.com',
    original_utm_campaign_v2: 'original_utm_campaign_v2',
    original_utm_channel: 'original_utm_channel',
    original_utm_medium_v2: 'original_utm_medium_v2',
    original_utm_source_v2: 'original_utm_source_v2',
    original_utm_term_v2: 'original_utm_term_v2',
    // Срок жизни кук в днях. Каждая запись, в том числе продление при прямом
    // заходе, отсчитывает его заново.
    windowDays: 720,
    // Заглушка пустого поля: её ждут существующие отчёты.
    NOT_SET: '(not_set)',
    // Предел нового значения в байтах UTF-8: без него одна длинная ссылка раздувает
    // куки на все поддомены, и сервер отвечает 400 на слишком большой заголовок.
    MAX_VALUE_BYTES: 1000,

    OWN_HOSTS: ['example.com'].concat(PAYMENT_HOSTS),

    // Зоны из двух частей. Без этого списка yandex.com.tr свернулся бы в com.tr
    MULTI_LABEL_ZONES: ['com.tr', 'com.ua', 'com.br', 'co.uk', 'co.il', 'co.jp'],

    // Органика: домен реферера без поддоменов -> имя источника
    ORGANIC_HOSTS: {
      'yandex.ru': 'yandex',
      'yandex.by': 'yandex',
      'yandex.kz': 'yandex',
      'yandex.uz': 'yandex',
      'yandex.com': 'yandex',
      'yandex.com.tr': 'yandex',
      'ya.ru': 'yandex',
      'google.com': 'google',
      'google.ru': 'google',
      'google.by': 'google',
      'google.kz': 'google',
      'mail.ru': 'mail',
      'rambler.ru': 'rambler',
      'bing.com': 'bing',
      'duckduckgo.com': 'duckduckgo'
    },

    // Значения utm_medium в нижнем регистре. Сравнение точное, не по подстроке.
    INTERNAL_MEDIUM: ['internal'],

    // Бизнес-канал: вторая, более дробная классификация того же касания. Нужна,
    // чтобы кука стыковалась с уже существующими отчётами.
    // Партнёрский лендинг: канал задаёт сам адрес входа, меток на нём может не быть.
    PARTNER_PATHS: { '/partner-landing': 'marketplace' },
    // Точное соответствие сразу по трём меткам: одного source мало, под 'marketplace'
    // может прийти и чужая кампания.
    PARTNER_CHANNELS: {
      'marketplace': { source: 'marketplace', medium: 'banner', campaign: 'spring_int_promo', channel: 'marketplace' },
      'retailer': { source: 'retailer', medium: 'retailer', campaign: 'retailer', channel: 'retailer' },
      'bank': { source: 'bank', medium: 'bank', campaign: 'bank', channel: 'bank' },
      'leads': { source: 'leads', medium: 'leads', campaign: 'leads_home', channel: 'leads' },
      'sms': { source: 'sms', medium: 'notification', campaign: 'sms_promo', channel: 'web_sms' }
    },
    // Правила по кампании, medium или source. Проверяются после точных соответствий
    REGEX_CHANNELS: [
      { channel: 'web_perf_search', campaign: /_search_/, source: /_search/ },
      { channel: 'web_perf_network', campaign: /_network_/, source: /_network/ },
      { channel: 'web_cpa', campaign: /_cpa/, medium: /cpa/ },
      { channel: 'web_reg', campaign: /reg_/ },
      { channel: 'web_media', campaign: /_int_/ },
      { channel: 'web_smm', campaign: /_smm_/ },
      { channel: 'web_dealer', campaign: /dealer/i, medium: /dealer/i, source: /dealer/i },
      { channel: 'web_geoservices', source: /maps|2gis/i }
    ],
    BUSINESS_ORGANIC: 'web_organic',
    BUSINESS_DEFAULT: 'web_other'
  };

  // Поле касания -> имя куки, в которую оно пишется.
  const COOKIE_OF = {
    source: CONFIG.original_utm_source_v2,
    medium: CONFIG.original_utm_medium_v2,
    campaign: CONFIG.original_utm_campaign_v2,
    term: CONFIG.original_utm_term_v2,
    channel: CONFIG.original_utm_channel
  };

  // 864e5 - миллисекунд в сутках.
  const WINDOW_MS = CONFIG.windowDays * 864e5;

  // Значение куки по имени как есть, без декодирования, или null, если её нет.
  // document.cookie отдаёт все куки одной строкой вида "a=1; b=2". Сырое значение
  // нужно продлению: оно пишет куку обратно в том виде, в каком её записали,
  // и битая %-кодировка не роняет тег.
  function readCookie(name) {
    const pair = document.cookie.split('; ').find(p => p.indexOf(name + '=') === 0);
    return pair === undefined ? null : pair.slice(name.length + 1);
  }

  // Записывает куку на весь домен сайта. value пишется как есть: новое значение
  // готовит clip, при продлении - сырое из куки. Срок - от текущего момента,
  // поэтому каждая запись продлевает жизнь куки.
  // SameSite=Lax: кука уходит при обычном переходе по ссылке с другого сайта,
  // но не в чужих фоновых запросах. Secure: только по HTTPS.
  function writeCookie(name, value, nowMs) {
    document.cookie = name + '=' + value +
      '; domain=' + CONFIG.cookieDomain +
      '; path=/' +
      '; expires=' + new Date(nowMs + WINDOW_MS).toUTCString() +
      '; SameSite=Lax; Secure';
  }

  // Домен реферера без поддоменов: shop.example.com -> example.com, www.yandex.ru -> yandex.ru.
  // Для зон из двух частей берётся три метки: yandex.com.tr, а не com.tr.
  // Пустой реферер, не-адрес -> null, адрес без хоста (about:blank) -> '': оба
  // считаются прямым заходом.
  // Проверка Тег Менеджера смотрит только синтаксис: catch {} она отвергла, методы вроде includes не ловит.
  function refDomain(referrer) {
    let parts;
    try {
      parts = new URL(referrer).hostname.toLowerCase().split('.');
    } catch (e) {
      return null;
    }
    const tail = parts.slice(-2).join('.');
    return parts.slice(CONFIG.MULTI_LABEL_ZONES.indexOf(tail) !== -1 ? -3 : -2).join('.');
  }

  // Новое значение как есть, без %-кодирования, пустое - NOT_SET.
  // ";" и управляющие символы вырезаются: они рвут строку куки. Длина режется
  // по целым символам до MAX_VALUE_BYTES байт в UTF-8 - так браузер хранит кириллицу.
  function clip(value) {
    let bytes = 0;
    let out = '';
    for (const ch of value.replace(/[;\x00-\x1f\x7f]/g, '')) {
      const cp = ch.codePointAt(0);
      bytes += cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
      if (bytes > CONFIG.MAX_VALUE_BYTES) break;
      out += ch;
    }
    return out.trim() || CONFIG.NOT_SET;
  }

  // Бизнес-канал касания: адрес лендинга, партнёрский справочник, правила
  // по подстроке, затем органика. Отвечает на вопрос "к какой строке отчёта
  // относится визит" и пишется в original_utm_channel.
  function businessChannel(touch, pathname) {
    const path = Object.keys(CONFIG.PARTNER_PATHS).find(p => pathname.indexOf(p) !== -1);
    if (path) return CONFIG.PARTNER_PATHS[path];

    const partner = Object.prototype.hasOwnProperty.call(CONFIG.PARTNER_CHANNELS, touch.source)
      ? CONFIG.PARTNER_CHANNELS[touch.source]
      : null;
    if (partner && partner.medium === touch.medium && partner.campaign === touch.campaign) {
      return partner.channel;
    }

    const rule = CONFIG.REGEX_CHANNELS.find(r =>
      (r.campaign && r.campaign.test(touch.campaign)) ||
      (r.medium && r.medium.test(touch.medium)) ||
      (r.source && r.source.test(touch.source)));
    if (rule) return rule.channel;

    return ['(none)', 'organic', 'referral'].indexOf(touch.medium) !== -1
      ? CONFIG.BUSINESS_ORGANIC
      : CONFIG.BUSINESS_DEFAULT;
  }

  // Одно срабатывание. Проверки идут строго по порядку, первая сработавшая решает.
  function run() {
    const url = new URL(location.href);
    const params = url.searchParams;
    const ref = refDomain(document.referrer);

    // 1. Переход со своего сайта - внутренний: ничего не пишем и не продлеваем.
    if (CONFIG.OWN_HOSTS.indexOf(ref) !== -1) return;

    const source = params.get('utm_source');
    const medium = params.get('utm_medium');
    const campaign = params.get('utm_campaign');
    const term = params.get('utm_term');
    // Внутреннюю метку ставит сам сайт, а реферер у неё теряется (новая вкладка,
    // noreferrer, копия ссылки), поэтому она идёт веткой прямого захода.
    // Только по точному utm_medium: кампании вида spring_int_... бывают партнёрскими.
    const internal = CONFIG.INTERNAL_MEDIUM.indexOf((medium || '').toLowerCase()) !== -1;
    // Новое касание. null - продление: источник остаётся прежним.
    let touch = null;

    if (!internal && (source || medium || campaign || term)) {
      // 2. Любая из четырёх UTM-меток главнее реферера: у рекламы реферер - поисковик
      //    или площадка, и по нему клик стал бы органикой. medium уходит в куку как пришёл.
      touch = {
        source: source || CONFIG.NOT_SET,
        medium: medium || CONFIG.NOT_SET,
        campaign: campaign || CONFIG.NOT_SET,
        term: term || CONFIG.NOT_SET
      };
    } else if (!internal && ref) {
      // 3. Меток нет, реферер есть - органика по мапе или referral с доменом в source.
      // hasOwnProperty, а не ORGANIC_HOSTS[ref]: иначе домен вроде 'constructor'
      // нашёлся бы в прототипе объекта и стал бы органикой.
      const organic = Object.prototype.hasOwnProperty.call(CONFIG.ORGANIC_HOSTS, ref);
      touch = {
        source: organic ? CONFIG.ORGANIC_HOSTS[ref] : ref,
        medium: organic ? 'organic' : 'referral',
        campaign: CONFIG.NOT_SET,
        term: CONFIG.NOT_SET
      };
    } else if (!readCookie(CONFIG.original_utm_source_v2)) {
      // 4. Ни меток, ни реферера, куки нет - первый прямой заход.
      touch = { source: '(direct)', medium: '(none)', campaign: CONFIG.NOT_SET, term: CONFIG.NOT_SET };
    }
    // Иначе кука есть и touch остался null: каждая из пяти кук, какая есть,
    // пишется обратно своим значением - срок продлён, ничего не изменилось.
    if (touch) touch.channel = businessChannel(touch, url.pathname);

    const now = Date.now();
    Object.keys(COOKIE_OF).forEach(field => {
      const name = COOKIE_OF[field];
      const value = touch ? clip(touch[field]) : readCookie(name);
      if (value !== null) writeCookie(name, value, now);
    });
  }

  // Тег не должен ломать страницу: любая ошибка остаётся внутри.
  try {
    run();
  } catch (e) {
  }
})();
