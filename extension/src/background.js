// Toolbar button and right-click menu: both open the downloader page in a tab,
// with the SoundCloud link already filled in when there is one.

const ext = globalThis.browser ?? globalThis.chrome;
const SOUNDCLOUD = /^https:\/\/([a-z0-9-]+\.)?soundcloud\.com\/./i;
const PATTERNS = ['https://soundcloud.com/*', 'https://*.soundcloud.com/*'];

function openApp(url) {
  const page = ext.runtime.getURL('app.html');
  ext.tabs.create({ url: url && SOUNDCLOUD.test(url) ? `${page}?url=${encodeURIComponent(url)}` : page });
}

ext.action.onClicked.addListener((tab) => openApp(tab?.url));

ext.runtime.onInstalled.addListener(async () => {
  await ext.contextMenus.removeAll();
  ext.contextMenus.create({
    id: 'scd-link', title: 'Download as MP3', contexts: ['link'], targetUrlPatterns: PATTERNS,
  });
  ext.contextMenus.create({
    id: 'scd-page', title: 'Download this as MP3', contexts: ['page'], documentUrlPatterns: PATTERNS,
  });
});

ext.contextMenus.onClicked.addListener((info) => openApp(info.linkUrl || info.pageUrl));
