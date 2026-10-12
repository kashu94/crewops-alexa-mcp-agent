// CrewOps desktop alerts: shows the notification the notifier pushed (encrypted for this
// browser alone), and opens the disruption when it is clicked. Nothing else runs here.

/** A page of this site, or the home page: never another site, however the path is written. */
function samePage(value) {
  try {
    const url = new URL(typeof value === "string" ? value : "/", self.location.origin);
    return url.origin === self.location.origin ? url.pathname + url.search : "/";
  } catch {
    return "/";
  }
}

// Take charge of tabs opened before this worker, so a click can bring one to the page.
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {};
  }
  const url = samePage(data.url);
  event.waitUntil(
    self.registration.showNotification(data.title || "CrewOps", {
      body: data.body || "A disruption needs you.",
      tag: data.tag || undefined,
      renotify: Boolean(data.tag),
      requireInteraction: true,
      data: { url },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(samePage(event.notification.data?.url), self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((open) => {
      const tab = open.find((c) => new URL(c.url).origin === self.location.origin);
      if (tab) {
        return tab
          .focus()
          .then(() => tab.navigate(url))
          .catch(() => self.clients.openWindow(url));
      }
      return self.clients.openWindow(url);
    }),
  );
});
