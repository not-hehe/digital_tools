const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Бизнес-канал отдельным файлом: справочник партнёров и правила меняются чаще,
// чем сама логика тега, и правятся вместе со своими проверками.
const FILES = ['attr.js', 'attr.min.js'];
// Если attr.min.js не собран, readFileSync бросит и тесты упадут, а не пропустятся.
const SOURCES = {};
FILES.forEach(file => {
  SOURCES[file] = fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
});
const NOW = Date.UTC(2026, 8, 22, 12, 0, 0);

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

function businessOf(page) {
  const line = page.writes.find(w => w.indexOf('original_utm_channel=') === 0);
  assert.ok(line, 'тег не записал original_utm_channel');
  const pair = line.split('; ')[0];
  return pair.slice(pair.indexOf('=') + 1);
}

FILES.forEach(file => {
  const createPage = opts => makePage(SOURCES[file], opts);

  test(file + ': AC-31: бизнес-канал по партнёрскому справочнику, регуляркам и органике', () => {
    const cases = [
      // Точное совпадение по трём меткам.
      { query: 'utm_source=marketplace&utm_medium=banner&utm_campaign=spring_int_promo', business: 'marketplace' },
      // Тот же source с чужой кампанией справочнику не подходит: остаётся правило по подстроке.
      { query: 'utm_source=marketplace&utm_medium=cpc&utm_campaign=other_int_promo', business: 'web_media' },
      // Совпали source и medium, кампания чужая - тоже не партнёр: сверяются все три метки.
      { query: 'utm_source=marketplace&utm_medium=banner&utm_campaign=other', business: 'web_other' },
      { query: 'utm_source=x&utm_medium=cpc&utm_campaign=y_search_z', business: 'web_perf_search' },
      { query: 'utm_source=x&utm_medium=cpa&utm_campaign=y', business: 'web_cpa' },
      { query: 'utm_source=2gis&utm_medium=cpc&utm_campaign=y', business: 'web_geoservices' },
      // Органика и незнакомая платная метка.
      { query: 'utm_source=x&utm_medium=organic&utm_campaign=y', business: 'web_organic' },
      { query: 'utm_source=x&utm_medium=cpc&utm_campaign=y', business: 'web_other' },
      // Метки без medium - не органика: web_other, так их ждут существующие отчёты.
      { query: 'utm_source=x', business: 'web_other' },
      { query: 'utm_campaign=y', business: 'web_other' }
    ];

    cases.forEach(c => {
      const page = createPage({ href: 'https://example.com/shop?' + c.query });
      page.fire();
      assert.equal(businessOf(page), c.business, 'бизнес-канал для ' + c.query);
    });

    // Партнёрский лендинг: канал задаёт адрес входа, меток может не быть вовсе.
    const landing = createPage({ href: 'https://example.com/partner-landing' });
    landing.fire();
    assert.equal(businessOf(landing), 'marketplace', 'бизнес-канал по адресу лендинга');
    // Путь ищется подстрокой: вложенная страница лендинга - тот же канал.
    const nested = createPage({ href: 'https://example.com/partner-landing/page' });
    nested.fire();
    assert.equal(businessOf(nested), 'marketplace', 'бизнес-канал по вложенной странице лендинга');

    // Переход из поиска без меток.
    const organic = createPage({ href: 'https://example.com/', referrer: 'https://yandex.ru/' });
    organic.fire();
    assert.equal(businessOf(organic), 'web_organic', 'бизнес-канал для органики');
  });
});
