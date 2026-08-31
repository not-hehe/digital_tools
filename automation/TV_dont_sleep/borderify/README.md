# TV Don't Sleep

Карусель вкладок Firefox для ТВ-дашбордов: по таймеру листает вкладки текущего
окна, обновляет каждую перед показом и не даёт экрану уснуть.

Зачем это нужно и как пользоваться, описано уровнем выше, в
[README проекта](../README.md).

## Установка

Временная, для проверки:

1. Открыть about:debugging#/runtime/this-firefox
2. Загрузить временное дополнение, выбрать manifest.json
3. Слетает при перезапуске Firefox

Постоянная: подписать пакет на
[addons.mozilla.org](https://addons.mozilla.org/developers/) в режиме
self-distribution, либо использовать Firefox Developer Edition или ESR
с настройкой xpinstall.signatures.required=false.

## Настройки

about:addons, найти TV Don't Sleep, открыть настройки. Задаются интервал показа
вкладки в минутах и пауза после обновления страницы.

## Screen Wake Lock

Расширение запрашивает Screen Wake Lock на активной вкладке, нужен Firefox 126
или новее. В MV3 доступ к сайтам выдаётся вручную: about:addons, TV Don't Sleep,
Разрешения, включить доступ к данным на всех сайтах.

## Структура

```
manifest.json     MV3, Firefox
background.js     карусель на alarms
options.html/js   настройки
wakelock.js       content script, держит экран включённым
icons/            иконки 16, 32, 48, 96, 128
```
