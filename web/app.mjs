export function matches(record, filters) {
  const query = (filters.q || "").trim().toLowerCase();
  return (!query || record.search.toLowerCase().includes(query))
    && (!filters.type || record.type === filters.type)
    && (!filters.cause || record.cause === filters.cause)
    && (!filters.kind || record.kind === filters.kind)
    && (!filters.from || record.date >= filters.from)
    && (!filters.to || record.date <= filters.to);
}

if (typeof document !== "undefined") {
  const form = document.querySelector("#filters");
  const cards = [...document.querySelectorAll(".incident-card")];
  const count = document.querySelector("#result-count");
  const empty = document.querySelector("#empty-results");
  const filter = () => {
    const values = Object.fromEntries(new FormData(form));
    let visible = 0;
    for (const card of cards) {
      card.hidden = !matches(card.dataset, values);
      if (!card.hidden) visible += 1;
    }
    count.textContent = `${visible} of ${cards.length} entries`;
    empty.hidden = visible !== 0;
  };
  form.hidden = false;
  form.addEventListener("input", filter);
  form.addEventListener("submit", (event) => event.preventDefault());
  form.addEventListener("reset", () => setTimeout(filter, 0));
  const revealCase = (id) => {
    const card = document.getElementById(id);
    if (card?.classList.contains("incident-card")) {
      form.reset();
      for (const element of form.elements) {
        if (element.name) element.value = "";
      }
      filter();
      card.querySelector("details.lesson").open = true;
    }
  };
  document.querySelectorAll("[data-case-link]").forEach((link) => {
    link.addEventListener("click", () => revealCase(link.hash.slice(1)));
  });
  if (location.hash) revealCase(location.hash.slice(1));
  filter();

  const timestamp = document.querySelector("#collected-at").dateTime;
  const age = Date.now() - Date.parse(timestamp);
  if (!Number.isFinite(age) || age > 48 * 60 * 60 * 1000) {
    const warning = document.querySelector("#stale-warning");
    warning.hidden = false;
    warning.textContent = "Collection is missing or more than 48 hours old. These are archived reports, not a current incident bulletin. Check source health and workflow history.";
  }
}
