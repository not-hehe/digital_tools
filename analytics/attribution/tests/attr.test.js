const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const FILES = ['attr.js', 'attr.min.js'];
// Если attr.min.js не собран, readFileSync бросит и тесты упадут, а не пропустятся.
const SOURCES = {};
FILES.forEach(file => {
  SOURCES[file] = fs.readFileSync(path.join(ROOT, file), 'utf8');
});
const NOW = Date.UTC(2026, 8, 22, 12, 0, 0);
const DAY = 864e5;

const NAMES = {
  source: 'original_utm_source_v2',
  medium: 'original_utm_medium_v2',
  campaign: 'original_utm_campaign_v2',
  term: 'original_utm_term_v2',
  channel: 'original_utm_channel'
};

// Файл выполняется как есть, в том виде, в каком его вставляют в тег:
// глобальные location, document и Date подменяются в песочнице vm.
function makePage(source, { href, referrer = '', cookie = '', now = NOW }) {
  const page = { now, cookie, writes: [] };

  class FakeDate extends Date {
    static now() {
      return page.now;
    }
  }

  const context = vm.createContext({ URL, Date: FakeDate });
  context.location = { href };
  context.document = {
    referrer,
    get cookie() {
      return page.cookie;
    },
    set cookie(value) {
      page.writes.push(value);
    }
  };

  page.fire = () => vm.runInContext(source, context);
  return page;
}

// Значения из строк записи: { имя куки: значение }. Значения пишутся как есть.
function rawOf(page) {
  const raw = {};
  page.writes.forEach(line => {
    const pair = line.split('; ')[0];
    const eq = pair.indexOf('=');
    raw[pair.slice(0, eq)] = pair.slice(eq + 1);
  });
  return raw;
}

// Касание, собранное обратно из пяти записанных кук.
function touchOf(page) {
  assert.equal(page.writes.length, 5, 'ожидалось пять записей: ' + page.writes.join(' | '));
  const raw = rawOf(page);
  const touch = {};
  Object.keys(NAMES).forEach(field => {
    touch[field] = raw[NAMES[field]];
  });
  return touch;
}

const DIRECT = {
  source: '(direct)',
  medium: '(none)',
  campaign: '(not_set)',
  term: '(not_set)',
  channel: 'web_organic'
};

// Куки, которые уже стоят у посетителя, в том формате, что ждут отчёты: кириллица как есть.
const SAVED_RAW = {
  original_utm_source_v2: 'yandex',
  original_utm_medium_v2: 'cpc',
  original_utm_campaign_v2: 'осень',
  original_utm_term_v2: 'tariff',
  original_utm_channel: 'web_other'
};

function cookieString(raw) {
  return ['a=1'].concat(Object.keys(raw).map(name => name + '=' + raw[name])).join('; ');
}

const SAVED = cookieString(SAVED_RAW);

// Байты значения в UTF-8 - так браузер считает размер куки.
function utf8Bytes(value) {
  return Buffer.byteLength(value, 'utf8');
}


FILES.forEach(file => {
  const createPage = opts => makePage(SOURCES[file], opts);

  test(file + ': AC-21: UTM-метки главнее реферера', () => {
    const page = createPage({
      href: 'https://example.com/tariffs?utm_source=yandex&utm_medium=cpc&utm_campaign=autumn&utm_term=tariff&x=1',
      referrer: 'https://yandex.ru/'
    });
    page.fire();

    assert.deepEqual(touchOf(page), {
      source: 'yandex',
      medium: 'cpc',
      campaign: 'autumn',
      term: 'tariff',
      channel: 'web_other'
    });
  });

  test(file + ': AC-22: неполные метки', () => {
    const cases = [
      { query: 'utm_source=vk', source: 'vk', medium: '(not_set)', campaign: '(not_set)', term: '(not_set)' },
      { query: 'utm_medium=CPC', source: '(not_set)', medium: 'CPC', campaign: '(not_set)', term: '(not_set)' },
      // Пустая метка - то же, что её нет: без реферера и кук это прямой заход.
      { query: 'utm_source=', source: '(direct)', medium: '(none)', campaign: '(not_set)', term: '(not_set)' },
      // Метка - любая из четырёх utm: одних campaign и term достаточно.
      { query: 'utm_campaign=x&utm_term=y', source: '(not_set)', medium: '(not_set)', campaign: 'x', term: 'y' },
      { query: 'utm_campaign=x', source: '(not_set)', medium: '(not_set)', campaign: 'x', term: '(not_set)' },
      { query: 'utm_term=y', source: '(not_set)', medium: '(not_set)', campaign: '(not_set)', term: 'y' }
    ];

    cases.forEach(c => {
      const page = createPage({ href: 'https://example.com/?' + c.query });
      page.fire();
      const touch = touchOf(page);
      delete touch.channel;
      const expected = { source: c.source, medium: c.medium, campaign: c.campaign, term: c.term };
      assert.deepEqual(touch, expected, 'для ' + c.query);
    });
  });

  test(file + ': AC-23: органика по домену без поддоменов, остальное referral', () => {
    const cases = [
      { referrer: 'https://www.yandex.ru/search/?text=x', source: 'yandex', medium: 'organic' },
      { referrer: 'https://images.yandex.ru/', source: 'yandex', medium: 'organic' },
      { referrer: 'https://ya.ru/', source: 'yandex', medium: 'organic' },
      { referrer: 'https://yandex.kz/', source: 'yandex', medium: 'organic' },
      // Зона из двух частей: домен второго уровня здесь - com.tr, нужен третий уровень.
      { referrer: 'https://www.yandex.com.tr/', source: 'yandex', medium: 'organic' },
      { referrer: 'https://news.google.com/', source: 'google', medium: 'organic' },
      { referrer: 'https://go.mail.ru/search', source: 'mail', medium: 'organic' },
      // Исключений для веб-почты больше нет: она сворачивается в домен поисковика.
      { referrer: 'https://mail.yandex.ru/', source: 'yandex', medium: 'organic' },
      { referrer: 'https://e.mail.ru/', source: 'mail', medium: 'organic' },
      { referrer: 'https://notyandex.ru/', source: 'notyandex.ru', medium: 'referral' },
      { referrer: 'https://m.vk.com/', source: 'vk.com', medium: 'referral' },
      { referrer: 'https://www.bbc.co.uk/news', source: 'bbc.co.uk', medium: 'referral' },
      { referrer: 'https://constructor/', source: 'constructor', medium: 'referral' },
      // Приложение Google на Android органикой не считается.
      { referrer: 'android-app://com.google.android.googlequicksearchbox/', source: 'android.googlequicksearchbox', medium: 'referral' }
    ];

    cases.forEach(c => {
      // utm в адресе нет: решает реферер, campaign и term - заглушки.
      const page = createPage({ href: 'https://example.com/?x=1', referrer: c.referrer });
      page.fire();
      const touch = touchOf(page);
      assert.equal(touch.source, c.source, 'source для ' + c.referrer);
      assert.equal(touch.medium, c.medium, 'medium для ' + c.referrer);
      assert.equal(touch.campaign, '(not_set)', 'campaign для ' + c.referrer);
      assert.equal(touch.term, '(not_set)', 'term для ' + c.referrer);
    });
  });

  test(file + ': AC-22: любая из четырёх utm главнее реферера', () => {
    const page = createPage({ href: 'https://example.com/?utm_term=y', referrer: 'https://yandex.ru/' });
    page.fire();
    assert.deepEqual(touchOf(page), {
      source: '(not_set)',
      medium: '(not_set)',
      campaign: '(not_set)',
      term: 'y',
      // Метки без medium - не органика: канал web_other.
      channel: 'web_other'
    });

    const campaign = createPage({ href: 'https://example.com/?utm_campaign=promo', referrer: 'https://yandex.ru/' });
    campaign.fire();
    assert.deepEqual(touchOf(campaign), {
      source: '(not_set)',
      medium: '(not_set)',
      campaign: 'promo',
      term: '(not_set)',
      channel: 'web_other'
    });
  });

  test(file + ': AC-24: click id не обрабатывается', () => {
    const yclid = createPage({ href: 'https://example.com/?yclid=123', referrer: 'https://yandex.ru/' });
    yclid.fire();
    const touch = touchOf(yclid);
    assert.equal(touch.source, 'yandex');
    assert.equal(touch.medium, 'organic');

    const gclid = createPage({ href: 'https://example.com/?gclid=abc' });
    gclid.fire();
    assert.deepEqual(touchOf(gclid), DIRECT);
  });

  test(file + ': AC-25: прямой заход без кук', () => {
    const page = createPage({ href: 'https://example.com/shop' });
    page.fire();
    assert.deepEqual(touchOf(page), DIRECT);
  });

  test(file + ': AC-26: прямой заход с куками продлевает их без изменений', () => {
    const page = createPage({ href: 'https://example.com/shop', cookie: SAVED });
    page.fire();

    assert.equal(page.writes.length, 5);
    assert.deepEqual(rawOf(page), SAVED_RAW);
    const expires = 'expires=' + new Date(NOW + 720 * DAY).toUTCString();
    page.writes.forEach(line => assert.equal(line.split('; ')[3], expires, line));
  });

  test(file + ': AC-26: продлеваются только те куки, что есть', () => {
    const raw = Object.assign({}, SAVED_RAW);
    delete raw.original_utm_term_v2;
    const page = createPage({ href: 'https://example.com/shop', cookie: cookieString(raw) });
    page.fire();

    assert.equal(page.writes.length, 4);
    assert.deepEqual(rawOf(page), raw);
  });

  test(file + ': AC-26: значение длиннее предела и битая %-последовательность продлеваются как есть', () => {
    const raw = Object.assign({}, SAVED_RAW, {
      original_utm_campaign_v2: '%E0%A4%A',
      original_utm_term_v2: 'я'.repeat(800)
    });
    const page = createPage({ href: 'https://example.com/shop', cookie: cookieString(raw) });
    page.fire();
    assert.deepEqual(rawOf(page), raw);
  });

  test(file + ': AC-26: без source-куки или с пустой - как без кук', () => {
    const noSource = Object.assign({}, SAVED_RAW);
    delete noSource.original_utm_source_v2;
    const emptySource = Object.assign({}, SAVED_RAW, { original_utm_source_v2: '' });

    [noSource, emptySource].forEach(raw => {
      const page = createPage({ href: 'https://example.com/shop', cookie: cookieString(raw) });
      page.fire();
      assert.deepEqual(touchOf(page), DIRECT, cookieString(raw));
    });
  });

  test(file + ': AC-38: кука с именем, которое начинается с нашего, не путается с нашей', () => {
    const lookalike = createPage({ href: 'https://example.com/shop', cookie: 'original_utm_source_v2_old=x' });
    lookalike.fire();
    assert.deepEqual(touchOf(lookalike), DIRECT, 'только похожая кука - это прямой заход без кук');

    const both = createPage({
      href: 'https://example.com/shop',
      cookie: 'original_utm_source_v2_old=x; ' + SAVED
    });
    both.fire();
    assert.deepEqual(rawOf(both), SAVED_RAW, 'продлевается наша кука, а не похожая');
  });

  test(file + ': AC-27: значимое касание при куках перезаписывает все пять', () => {
    const referral = createPage({ href: 'https://example.com/shop', referrer: 'https://habr.com/post', cookie: SAVED });
    referral.fire();
    assert.deepEqual(touchOf(referral), {
      source: 'habr.com',
      medium: 'referral',
      campaign: '(not_set)',
      term: '(not_set)',
      channel: 'web_organic'
    });

    const utm = createPage({ href: 'https://example.com/shop?utm_source=vk&utm_medium=social', cookie: SAVED });
    utm.fire();
    const touch = touchOf(utm);
    assert.equal(touch.source, 'vk');
    assert.equal(touch.medium, 'social');
    assert.equal(touch.campaign, '(not_set)');
  });

  test(file + ': AC-28: переход со своего хоста не пишет ничего', () => {
    const referrers = ['https://example.com/', 'https://shop.example.com/', 'https://www.example.com/x'];
    const hrefs = ['https://example.com/shop', 'https://example.com/shop?utm_source=vk&utm_medium=social'];

    referrers.forEach(referrer => {
      hrefs.forEach(href => {
        ['', SAVED].forEach(cookie => {
          const page = createPage({ href, referrer, cookie });
          page.fire();
          assert.equal(page.writes.length, 0, referrer + ' -> ' + href + (cookie ? ', с куками' : ', без кук'));
        });
      });
    });
  });

  test(file + ': AC-29: utm_medium=internal - как прямой заход', () => {
    const hrefs = [
      'https://example.com/shop?utm_medium=internal&utm_source=banner',
      'https://example.com/shop?utm_medium=INTERNAL&utm_source=banner'
    ];

    hrefs.forEach(href => {
      ['', 'https://habr.com/'].forEach(referrer => {
        const withCookie = createPage({ href, referrer, cookie: SAVED });
        withCookie.fire();
        assert.deepEqual(rawOf(withCookie), SAVED_RAW, 'с куками: ' + href + ', реферер ' + referrer);

        const noCookie = createPage({ href, referrer });
        noCookie.fire();
        assert.deepEqual(touchOf(noCookie), DIRECT, 'без кук: ' + href + ', реферер ' + referrer);
      });
    });
  });

  test(file + ': AC-29: контроль ложных срабатываний на internal', () => {
    const promo = createPage({ href: 'https://example.com/shop?utm_medium=internal_promo&utm_source=x' });
    promo.fire();
    assert.equal(touchOf(promo).medium, 'internal_promo', 'internal_promo - обычная метка');

    const partner = createPage({ href: 'https://example.com/shop?utm_medium=cpc&utm_campaign=spring_int_promo' });
    partner.fire();
    const touch = touchOf(partner);
    assert.equal(touch.medium, 'cpc');
    assert.equal(touch.campaign, 'spring_int_promo', 'кампания с _int_ - не внутренняя метка');
  });

  test(file + ': AC-30: атрибуты записи и значение как есть', () => {
    const page = createPage({
      href: 'https://example.com/shop?utm_source=x&utm_medium=cpc&utm_campaign=%D0%BE%D1%81%D0%B5%D0%BD%D1%8C&utm_term=a+b'
    });
    page.fire();

    const expires = 'expires=' + new Date(NOW + 720 * DAY).toUTCString();
    assert.deepEqual(page.writes.map(line => line.split('; ')[0].split('=')[0]), [
      NAMES.source, NAMES.medium, NAMES.campaign, NAMES.term, NAMES.channel
    ]);
    page.writes.forEach(line => {
      assert.deepEqual(line.split('; ').slice(1), ['domain=.example.com', 'path=/', expires, 'SameSite=Lax', 'Secure'], line);
    });
    const raw = rawOf(page);
    assert.equal(raw[NAMES.campaign], 'осень', 'кириллица пишется как есть');
    assert.equal(raw[NAMES.term], 'a b');
  });

  test(file + ': AC-37: длина и символы нового значения', () => {
    const long = (value) => encodeURIComponent(value);
    const cases = [
      { label: 'латиница', value: 'a'.repeat(5000), bytes: 1000 },
      { label: 'кириллица', value: 'я'.repeat(5000), bytes: 1000 },
      // Эмодзи - 4 байта и пара суррогатов: резать можно только между символами.
      { label: 'эмодзи', value: '😀'.repeat(500), bytes: 1000 },
      // Предел не делится на размер символа: последний неполный символ отбрасывается.
      { label: 'кириллица с латиницей', value: 'a' + 'я'.repeat(5000), bytes: 999 }
    ];

    cases.forEach(c => {
      const page = createPage({ href: 'https://example.com/?utm_source=x&utm_campaign=' + long(c.value) + '&utm_term=' + long(c.value) });
      page.fire();
      const raw = rawOf(page);
      [NAMES.campaign, NAMES.term].forEach(name => {
        assert.equal(utf8Bytes(raw[name]), c.bytes, c.label + ': байт в ' + name);
        assert.ok(c.value.startsWith(raw[name]), c.label + ': значение - начало метки по целым символам');
      });
    });

    const header = createPage({
      href: 'https://example.com/?' + ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term']
        .map(k => k + '=' + long('я'.repeat(5000))).join('&')
    });
    header.fire();
    const total = Object.keys(rawOf(header)).reduce((sum, name) => sum + utf8Bytes(name + '=' + rawOf(header)[name]) + 2, 0);
    assert.ok(total < 4300, 'пять кук вместе - ' + total + ' байт');

    const unsafe = createPage({ href: 'https://example.com/?utm_source=a%3Bb%01c%7F&utm_medium=cpc' });
    unsafe.fire();
    assert.equal(rawOf(unsafe)[NAMES.source], 'abc', '";" и управляющие символы вырезаны');

    // Пустое после очистки значение - заглушка, а не пустая кука: иначе следующий прямой
    // заход счёл бы куку отсутствующей и перезаписал источник на direct.
    ['%3B', '%20%20', '%3B%20'].forEach(raw => {
      const empty = createPage({ href: 'https://example.com/?utm_source=' + raw + '&utm_medium=cpc' });
      empty.fire();
      assert.equal(rawOf(empty)[NAMES.source], '(not_set)', 'utm_source=' + raw);
    });
    const trimmed = createPage({ href: 'https://example.com/?utm_source=%20vk%20&utm_medium=cpc' });
    trimmed.fire();
    assert.equal(rawOf(trimmed)[NAMES.source], 'vk', 'пробелы по краям срезаны');
  });

  test(file + ': AC-32: ошибки не выходят наружу', () => {
    ['not a url', 'about:blank'].forEach(referrer => {
      const badReferrer = createPage({ href: 'https://example.com/shop', referrer });
      badReferrer.fire();
      assert.deepEqual(touchOf(badReferrer), DIRECT, 'реферер ' + referrer);
    });

    const badHref = createPage({ href: 'not a url', referrer: 'https://habr.com/' });
    assert.doesNotThrow(() => badHref.fire());
    assert.equal(badHref.writes.length, 0);
  });
});

// acorn берётся через npx: без сети его не скачать, и это не ошибка кода.
function runAcorn(ecma, file) {
  const res = spawnSync('npx', ['--yes', 'acorn', '--ecma' + ecma, '--silent', file],
    { cwd: ROOT, timeout: 60000, encoding: 'utf8' });
  const stderr = res.stderr || '';
  // Ошибку синтаксиса acorn печатает с позицией: "... (attr.js 5:2)".
  const syntaxError = /\(\S+ \d+:\d+\)/.test(stderr);
  const unavailable = !syntaxError && (Boolean(res.error) || res.status === null ||
    (res.status !== 0 && /npm (error|ERR!)|ENOTCACHED|ENOTFOUND|EAI_AGAIN|ETIMEDOUT|ECONNREFUSED|offline/i.test(stderr)));
  return { status: res.status, stderr, unavailable };
}

test('AC-33: синтаксис ES2015 через acorn', { timeout: 120000 }, (t) => {
  for (const file of FILES) {
    const es2015 = runAcorn(2015, file);
    if (es2015.unavailable) {
      t.skip('acorn недоступен: ' + es2015.stderr.split('\n')[0]);
      return;
    }
    assert.equal(es2015.status, 0, file + ' ecma2015: ' + es2015.stderr);
  }

  // Контроль: acorn вообще ловит синтаксис новее ES5. Для минифицированного файла не нужен.
  const es5 = runAcorn(5, 'attr.js');
  if (es5.unavailable) {
    t.skip('acorn недоступен: ' + es5.stderr.split('\n')[0]);
    return;
  }
  assert.notEqual(es5.status, 0, 'ecma5 должен упасть на синтаксисе новее ES5');
});

test('AC-35: сборка attr.min.js свежая, хеш attr.js в первой строке совпадает', () => {
  const hash = crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT, 'attr.js'))).digest('hex');
  const firstLine = SOURCES['attr.min.js'].split('\n')[0];
  assert.equal(firstLine, '/* sha256 ' + hash + ' */', 'attr.js изменён без пересборки: node build.js');
});
