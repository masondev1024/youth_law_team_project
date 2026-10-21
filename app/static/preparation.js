(() => {
  "use strict";

  const ISSUE_LABELS = {
    repayment: "갚는 부담이 커졌어요",
    collection: "상환요구 연락을 받았어요",
    procedure: "채무조정/개인회생을 잘 모르겠어요",
  };
  const OFFICIAL_HOSTS = ["kinfa.or.kr", "ccrs.or.kr", "klac.or.kr", "fss.or.kr", "fsc.go.kr", "scourt.go.kr", "law.go.kr"];
  const ITEM_KIND_LABELS = {
    official_guidance: "공식 안내",
    service_suggestion: "상담 준비 제안",
  };
  const REQUEST_TIMEOUT_MS = 5000;

  const issueButtons = Array.from(document.querySelectorAll("[data-issue]"));
  const factsFieldset = document.getElementById("facts-fieldset");
  const factsList = document.getElementById("facts-list");
  const createButton = document.getElementById("create-card");
  const resetButton = document.getElementById("reset-form");
  const resetAfterCardButton = document.getElementById("reset-after-card");
  const retryOptionsButton = document.getElementById("retry-options");
  const formStatus = document.getElementById("form-status");
  const cardRegion = document.getElementById("card-region");
  const cardSections = document.getElementById("card-sections");
  const cardStatus = document.getElementById("card-status");
  const cardIntro = document.getElementById("card-intro");
  const copyButton = document.getElementById("copy-card");
  const copyStatus = document.getElementById("copy-status");
  const manualCopy = document.getElementById("manual-copy");
  const manualCopyLabel = document.getElementById("manual-copy-label");

  let activeIssue = null;
  let options = { topics: [], facts: [], mutually_exclusive_fact_groups: [] };
  let optionsReady = false;
  let optionsLoading = false;
  const requestControllers = new Set();
  let requestGeneration = 0;
  let loadingCard = false;

  function setStatus(element, message) {
    element.textContent = message;
  }

  function makeElement(tagName, className, text) {
    const element = document.createElement(tagName);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function addTextLink(parent, label, rawUrl) {
    const url = safeHttpsUrl(rawUrl);
    if (!url) {
      parent.appendChild(makeElement("span", "", label));
      if (rawUrl) parent.appendChild(makeElement("span", "subtle-label", " (안전한 공식 주소를 확인할 수 없습니다)"));
      return;
    }
    const link = makeElement("a", "", label);
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    parent.appendChild(link);
  }

  function safeHttpsUrl(value) {
    if (typeof value !== "string" || value.length > 2048 || value !== value.trim()) return null;
    try {
      const url = new URL(value);
      const host = url.hostname.toLowerCase();
      const approvedHost = OFFICIAL_HOSTS.some((base) => host === base || host.endsWith(`.${base}`));
      if (url.protocol !== "https:" || !approvedHost || (url.port && Number(url.port) > 443) || url.username || url.password) return null;
      return url;
    } catch (_error) {
      return null;
    }
  }

  async function requestJson(url, init, generation) {
    const controller = new AbortController();
    requestControllers.add(controller);
    const timeoutId = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(url, {
        ...init,
        cache: "no-store",
        credentials: "same-origin",
        headers: { "Accept": "application/json", ...(init.headers || {}) },
        signal: controller.signal,
      });
      if (generation !== requestGeneration) return null;
      let payload = null;
      try { payload = await response.json(); } catch (_error) { /* Do not display server response text. */ }
      if (!response.ok) {
        const error = new Error("request_failed");
        error.status = response.status;
        throw error;
      }
      return payload;
    } finally {
      window.clearTimeout(timeoutId);
      requestControllers.delete(controller);
    }
  }

  function setLoading(isLoading) {
    loadingCard = isLoading;
    createButton.disabled = isLoading || !activeIssue;
    issueButtons.forEach((button) => { button.disabled = isLoading; });
    factsFieldset.disabled = isLoading || !activeIssue;
    createButton.textContent = isLoading ? "카드를 정리하고 있어요…" : "준비 카드 만들기";
  }

  function clearCard() {
    cardRegion.hidden = true;
    cardSections.replaceChildren();
    cardIntro.textContent = "";
    cardStatus.textContent = "";
    copyStatus.textContent = "";
    manualCopy.value = "";
    manualCopy.hidden = true;
    manualCopyLabel.hidden = true;
  }

  function renderFacts() {
    factsList.replaceChildren();
    if (!activeIssue) {
      factsList.appendChild(makeElement("p", "muted", "먼저 고민 유형을 선택해 주세요."));
      return;
    }
    const topic = options.topics.find((item) => item && item.id === activeIssue);
    if (topic && topic.description) {
      factsList.appendChild(makeElement("p", "topic-description", topic.description));
    }
    const eligibleFacts = options.facts.filter((fact) =>
      fact && typeof fact.id === "string" && Array.isArray(fact.topic_ids) && fact.topic_ids.includes(activeIssue)
    );
    if (!eligibleFacts.length) {
      factsList.appendChild(makeElement("p", "muted", "이 유형의 선택지를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."));
      return;
    }
    for (const fact of eligibleFacts) {
      const label = makeElement("label", "fact-option");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.name = "selected_facts";
      checkbox.value = fact.id;
      checkbox.addEventListener("change", (event) => {
        const changedCheckbox = event.currentTarget;
        let clearedConflict = false;
        if (changedCheckbox.checked) {
          for (const group of options.mutually_exclusive_fact_groups) {
            if (!Array.isArray(group) || !group.includes(changedCheckbox.value)) continue;
            for (const other of factsList.querySelectorAll('input[name="selected_facts"]:checked')) {
              if (other !== changedCheckbox && group.includes(other.value)) {
                other.checked = false;
                clearedConflict = true;
              }
            }
          }
        }
        clearCard();
        createButton.disabled = loadingCard || !activeIssue;
        setStatus(formStatus, clearedConflict
          ? "방금 선택한 사실을 남기고 함께 선택할 수 없는 기존 선택을 해제했습니다."
          : "");
      });
      label.append(checkbox, makeElement("span", "", fact.label));
      factsList.appendChild(label);
    }
  }

  function onIssueSelect(issue) {
    if (!ISSUE_LABELS[issue] || loadingCard || !optionsReady) return;
    const changed = activeIssue !== issue;
    activeIssue = issue;
    issueButtons.forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.issue === issue));
    });
    factsFieldset.disabled = false;
    if (changed) {
      renderFacts();
      clearCard();
      setStatus(formStatus, "유형을 바꾸면 앞서 고른 사실은 지워집니다. 해당하는 내용만 다시 골라 주세요.");
    }
    createButton.disabled = false;
  }

  function appendSection(title, content) {
    const section = makeElement("section", "card-section");
    section.appendChild(makeElement("h3", "", title));
    if (content) section.appendChild(content);
    cardSections.appendChild(section);
    return section;
  }

  function appendItems(title, items, fallback, sources) {
    const section = appendSection(title);
    if (!Array.isArray(items) || items.length === 0) {
      section.appendChild(makeElement("p", "muted", fallback));
      return;
    }
    const kinds = new Set(items.map((item) => item && ITEM_KIND_LABELS[item.kind] ? item.kind : "unknown"));
    const oneKnownKind = kinds.size === 1 && !kinds.has("unknown");
    if (oneKnownKind) {
      const kind = Array.from(kinds)[0];
      let description = "기관이 공개한 안내입니다. 연결된 출처와 확인일을 함께 확인하세요.";
      if (kind === "service_suggestion") {
        description = title.includes("자료")
          ? "상담에 도움이 될 수 있는 자료 후보입니다. 신청에 반드시 필요한 공식 서류 목록은 아닙니다."
          : title.includes("질문")
            ? "상담 담당자에게 물어볼 질문 제안입니다."
            : "상담 전에 확인하거나 단정하지 말아야 할 내용을 정리했습니다.";
      }
      const intro = makeElement("p", "item-kind-intro");
      intro.append(makeElement("span", `kind-label ${kind === "service_suggestion" ? "suggestion" : "official"}`, ITEM_KIND_LABELS[kind]), document.createTextNode(` ${description}`));
      section.appendChild(intro);
    }
    const list = document.createElement("ul");
    for (const item of items) {
      const li = document.createElement("li");
      const kind = item && ITEM_KIND_LABELS[item.kind];
      if (kind && !oneKnownKind) li.appendChild(makeElement("span", `kind-label ${item.kind === "service_suggestion" ? "suggestion" : "official"}`, kind));
      li.appendChild(makeElement("span", "", item && item.text ? item.text : "내용을 확인할 수 없습니다."));
      if (item && item.kind === "official_guidance" && item.source_id) {
        const source = sources.get(item.source_id);
        const citation = makeElement("span", "item-source");
        citation.appendChild(document.createTextNode(" · 출처: "));
        if (source) {
          addTextLink(citation, source.title || "공식 출처", source.url);
          if (source.checked_on) citation.appendChild(makeElement("span", "source-meta", ` · 확인일 ${source.checked_on} · 전문가 승인 아님`));
        } else {
          citation.appendChild(makeElement("span", "source-meta", "출처 정보를 확인할 수 없습니다."));
        }
        li.appendChild(citation);
      }
      list.appendChild(li);
    }
    section.appendChild(list);
  }

  function sourceIndex(card) {
    const index = new Map();
    if (Array.isArray(card.sources)) {
      for (const source of card.sources) {
        if (source && typeof source.id === "string") index.set(source.id, source);
      }
    }
    return index;
  }

  function renderRoute(route, sources) {
    const section = appendSection("먼저 문의해 볼 상담 창구");
    if (!route || typeof route !== "object") {
      section.appendChild(makeElement("p", "muted", "공식 상담 경로를 확인할 수 없습니다."));
      return;
    }
    const organization = makeElement("p");
    organization.append(makeElement("strong", "", "기관/창구: "), makeElement("span", "", route.organization || "확인 필요"));
    section.appendChild(organization);
    if (route.purpose) section.appendChild(makeElement("p", "", route.purpose));
    const source = route.source_id ? sources.get(route.source_id) : null;
    const address = makeElement("p");
    address.appendChild(makeElement("strong", "", "공식 웹 주소: "));
    addTextLink(address, source && source.title ? source.title : "공식 페이지 열기", route.url);
    section.appendChild(address);
    if (source && source.checked_on) section.appendChild(makeElement("p", "source-meta", `공식 페이지 원문 확인: ${source.checked_on} · 전문가가 승인한 법률 판단은 아닙니다.`));
  }

  function renderSelectedFacts(facts) {
    const section = appendSection("현재 선택한 사실");
    section.appendChild(makeElement("p", "subtle-label", "사용자가 직접 선택한 내용 · 미확인 사실이며, 정확성은 상담에서 다시 확인하세요."));
    if (!Array.isArray(facts) || facts.length === 0) {
      section.appendChild(makeElement("p", "muted", "선택한 사실이 없습니다."));
      return;
    }
    const wrap = makeElement("div");
    for (const fact of facts) wrap.appendChild(makeElement("span", "fact-tag", fact && fact.label ? fact.label : "선택한 사실"));
    section.appendChild(wrap);
  }

  function renderSources(sources) {
    const section = appendSection("출처와 확인일");
    if (!Array.isArray(sources) || sources.length === 0) {
      section.appendChild(makeElement("p", "muted", "출처 확인 정보를 불러오지 못했습니다."));
      return;
    }
    const list = makeElement("ol", "source-list");
    for (const source of sources) {
      const li = document.createElement("li");
      addTextLink(li, source && source.title ? source.title : "출처", source && source.url);
      const reviewText = source && source.review_kind === "official_page_check" ? "공식 페이지 원문 확인 (전문가 승인 아님)" : "확인 방식 정보 없음";
      li.appendChild(makeElement("span", "source-meta", `${reviewText}${source && source.checked_on ? ` · 확인일 ${source.checked_on}` : ""}`));
      list.appendChild(li);
    }
    section.appendChild(list);
  }

  function renderCard(card) {
    const sources = sourceIndex(card);
    cardSections.replaceChildren();
    cardStatus.textContent = card.status === "needs_details" ? "추가 확인 필요" : "초안";
    cardIntro.textContent = card.status === "needs_details"
      ? "고민 유형별 기본 안내입니다. 모르는 사실은 비워 두고 상담에서 확인하세요. 선택만으로 지원 자격이 정해지지는 않습니다."
      : "고민 유형별 기본 안내입니다. 고른 사실은 상담 준비 메모이며, 이 선택으로 지원 자격이나 상담 결과가 정해지지는 않습니다.";

    renderRoute(card.official_route, sources);
    renderSelectedFacts(card.selected_facts);
    appendItems("상담 전에 더 확인할 점", card.to_confirm, "현재 선택한 정보로는 더 확인할 내용을 정리하지 못했습니다.", sources);
    appendItems("상담에 도움이 될 수 있는 자료", card.preparation_items, "자료 후보가 없습니다. 자료를 준비하지 않아도 상담 기관에 먼저 문의할 수 있습니다.", sources);
    appendItems("상담 담당자에게 물어볼 질문", card.counselor_questions, "질문 제안을 확인할 수 없습니다.", sources);
    appendItems("아직 단정할 수 없는 내용", card.uncertainties, "자격이나 결과를 여기서 확정할 수 없습니다. 공식 상담에서 확인하세요.", sources);
    renderSources(card.sources);
    cardRegion.hidden = false;
    cardRegion.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function getSelectedFactIds() {
    return Array.from(factsList.querySelectorAll('input[name="selected_facts"]:checked')).map((input) => input.value);
  }

  function friendlyError(error) {
    if (error && error.name === "AbortError") return "요청 시간이 길어져 중단했습니다. 잠시 뒤 다시 시도해 주세요.";
    if (error && error.status === 422) return "선택 내용을 확인할 수 없습니다. 고민 유형과 사실 선택지를 다시 확인해 주세요.";
    if (error && error.status === 503) return "준비 카드 안내를 잠시 사용할 수 없습니다. 공식 상담 기관에 직접 문의하거나 잠시 뒤 다시 시도해 주세요.";
    return "카드를 만들지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해 주세요.";
  }

  async function createCard() {
    if (!activeIssue || loadingCard) return;
    const selectedFacts = getSelectedFactIds();
    const generation = ++requestGeneration;
    setLoading(true);
    clearCard();
    setStatus(formStatus, "선택한 내용으로 카드 초안을 정리하고 있습니다.");
    try {
      const card = await requestJson("/api/preparation/card", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ issue: activeIssue, selected_facts: selectedFacts }),
      }, generation);
      if (generation !== requestGeneration || !card) return;
      renderCard(card);
      setStatus(formStatus, "카드 초안이 준비되었습니다. 선택한 사실과 출처를 확인해 주세요.");
    } catch (error) {
      if (generation === requestGeneration) setStatus(formStatus, friendlyError(error));
    } finally {
      if (generation === requestGeneration) setLoading(false);
    }
  }

  function resetAll() {
    requestGeneration += 1;
    requestControllers.forEach((controller) => controller.abort());
    requestControllers.clear();
    loadingCard = false;
    activeIssue = null;
    issueButtons.forEach((button) => {
      button.disabled = !optionsReady;
      button.setAttribute("aria-pressed", "false");
    });
    factsFieldset.disabled = true;
    renderFacts();
    createButton.disabled = true;
    createButton.textContent = "준비 카드 만들기";
    clearCard();
    setStatus(formStatus, "선택과 카드 내용을 지웠습니다. 필요한 경우 다시 선택해 주세요.");
    if (!optionsReady) {
      optionsLoading = false;
      loadOptions();
    }
  }

  function cardAsPlainText() {
    const lines = ["첫 금융 상담 준비 카드", `고민 유형: ${ISSUE_LABELS[activeIssue] || "확인 필요"}`, cardIntro.textContent, ""];
    const heading = cardRegion.querySelectorAll(".card-section");
    heading.forEach((section) => {
      const sectionText = (section.innerText || section.textContent || "").trim();
      if (sectionText) lines.push(sectionText);
      const urls = Array.from(new Set(Array.from(section.querySelectorAll("a[href]"), (link) => link.href)));
      for (const url of urls) lines.push(`출처 URL: ${url}`);
      lines.push("");
    });
    lines.push("이 카드는 상담 준비를 돕는 안내이며, 법률 결론이나 자격을 확정하지 않습니다.");
    return lines.join("\n");
  }

  async function copyCard() {
    const text = cardAsPlainText();
    try {
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") throw new Error("clipboard_unavailable");
      await navigator.clipboard.writeText(text);
      manualCopy.hidden = true;
      manualCopyLabel.hidden = true;
      setStatus(copyStatus, "카드 내용을 복사했습니다.");
    } catch (_error) {
      manualCopy.value = text;
      manualCopy.hidden = false;
      manualCopyLabel.hidden = false;
      manualCopy.focus();
      manualCopy.select();
      setStatus(copyStatus, "자동 복사를 사용할 수 없습니다. 선택된 내용을 직접 복사해 주세요.");
    }
  }

  async function loadOptions() {
    if (optionsLoading) return;
    const generation = requestGeneration;
    optionsLoading = true;
    retryOptionsButton.disabled = true;
    retryOptionsButton.hidden = true;
    setStatus(formStatus, "선택지를 불러오는 중입니다.");
    try {
      const result = await requestJson("/api/preparation/options", { method: "GET" }, generation);
      if (!result || generation !== requestGeneration) return;
      if (!Array.isArray(result.topics) || !Array.isArray(result.facts)) throw new Error("invalid_options");
      options = {
        topics: result.topics,
        facts: result.facts,
        mutually_exclusive_fact_groups: Array.isArray(result.mutually_exclusive_fact_groups)
          ? result.mutually_exclusive_fact_groups
          : [],
      };
      optionsReady = true;
      issueButtons.forEach((button) => { button.disabled = false; });
      factsList.replaceChildren(makeElement("p", "muted", "먼저 고민 유형을 선택해 주세요."));
      setStatus(formStatus, "고민 유형을 고르면 해당하는 사실 선택지가 나타납니다.");
    } catch (error) {
      if (generation !== requestGeneration) return;
      factsList.replaceChildren(makeElement("p", "muted", "선택지를 불러오지 못했습니다."));
      setStatus(formStatus, friendlyError(error));
      retryOptionsButton.hidden = false;
    } finally {
      if (generation === requestGeneration) {
        optionsLoading = false;
        retryOptionsButton.disabled = false;
      }
    }
  }

  issueButtons.forEach((button) => button.addEventListener("click", () => onIssueSelect(button.dataset.issue)));
  createButton.addEventListener("click", createCard);
  resetButton.addEventListener("click", resetAll);
  resetAfterCardButton.addEventListener("click", resetAll);
  retryOptionsButton.addEventListener("click", loadOptions);
  copyButton.addEventListener("click", copyCard);
  loadOptions();
})();
