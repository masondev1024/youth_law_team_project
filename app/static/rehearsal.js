(() => {
  "use strict";

  const API = "/rehearsal/api";
  const SELECTS = {
    "task-status": [
      ["completed", "완료"], ["partial", "일부 수행"], ["abandoned", "자발 중단"],
      ["timeout", "6분 종료"], ["safety_stopped", "안전 사유 중단"],
      ["technical_failure", "기술 문제로 중단"], ["not_run", "과업 미실행"],
    ],
    "help-requested": [["not_observed", "관찰하지 않음"], ["yes", "예"], ["no", "아니요"], ["unclear", "불명확"]],
    "content-help": [["not_observed", "관찰하지 않음"], ["yes", "예"], ["no", "아니요"], ["unclear", "불명확"]],
    "technical-issue": [["not_observed", "관찰하지 않음"], ["yes", "예"], ["no", "아니요"], ["unclear", "불명확"]],
    "next-action": [["not_observed", "관찰하지 않음"], ["observed_understood", "이해한 것으로 관찰"], ["observed_misunderstood", "오해한 것으로 관찰"], ["unclear", "불명확"]],
    "source-understanding": [["not_observed", "관찰하지 않음"], ["observed_understood", "이해한 것으로 관찰"], ["observed_misunderstood", "오해한 것으로 관찰"], ["unclear", "불명확"]],
    "list-understanding": [["not_observed", "관찰하지 않음"], ["observed_understood", "이해한 것으로 관찰"], ["observed_misunderstood", "오해한 것으로 관찰"], ["unclear", "불명확"]],
    "limits-understanding": [["not_observed", "관찰하지 않음"], ["observed_understood", "이해한 것으로 관찰"], ["observed_misunderstood", "오해한 것으로 관찰"], ["unclear", "불명확"]],
    "critical-misunderstanding": [["not_observed", "관찰하지 않음"], ["yes", "예"], ["no_observed", "관찰된 바 없음"], ["unclear", "불명확"]],
    "critical-phase": [["unknown", "모름 또는 확인하지 않음"], ["during_task", "과업 중"], ["post_task", "과업 뒤 회고"], ["both", "과업 중과 회고 모두"]],
    "sensitive-data": [["not_observed", "관찰하지 않음"], ["incident_pending", "사건 발생, 확인 대기"], ["restricted_pending_deletion", "접근 제한, 삭제 확인 대기"], ["deletion_verified", "삭제 확인됨"], ["unknown", "알 수 없음"]],
  };
  const $ = (id) => document.getElementById(id);
  let state = null;
  let csrfToken = null;
  let busy = false;
  let timerStartedAt = null;
  let timerInterval = null;
  let cardDisplayedLocally = false;
  let firstCardDisplayedElapsed = null;
  let timingCheckpointPending = false;
  let taskStatusAfterCheckpoint = null;
  let lastSavedVersion = null;

  function setText(id, value) { $(id).textContent = value == null ? "" : String(value); }
  function status(id, message) { setText(id, message); }
  function setBusy(value) {
    busy = value;
    document.querySelectorAll("button, input, select").forEach((element) => {
      if (element.id === "login-token" && !$('workspace').hidden) return;
      element.disabled = value || element.dataset.disabledByState === "true";
    });
    updateConsentButton();
    if (!value && state && state.record) syncNotRunRules();
  }
  function updateConsentButton() {
    const complete = $("adult-consent").checked && $("participation-consent").checked && $("recording-consent").checked && $("task-select").value;
    $("accept-button").disabled = busy || !complete;
  }
  function showGlobal(message, focus = false) {
    status("global-status", message);
    if (focus) $("global-status").focus();
  }
  function errorMessage(code, httpStatus) {
    const messages = {
      request_timeout: "요청 시간이 초과됐습니다. 현재 상태를 다시 불러와 확인해 주세요.",
      storage_unavailable: "저장 기능을 일시적으로 사용할 수 없습니다. 잠시 뒤 다시 시도해 주세요.",
      invalid_request: "입력 내용을 확인해 주세요.", invalid_record: "기록을 확인할 수 없습니다.",
      conflict: "다른 변경과 충돌했습니다. 아래 버튼으로 현재 상태를 다시 불러오세요.",
      revoked: "이 기록은 철회되어 다시 열 수 없습니다.", expired: "보관 기간이 끝난 기록입니다.",
      admission_closed: "실제 참가자 등록은 닫혀 있습니다. 이 화면은 개발 리허설만 지원합니다.",
      origin_mismatch: "요청 출처를 확인할 수 없습니다. 같은 로컬 주소에서 다시 열어 주세요.",
      csrf_failed: "세션 확인이 만료됐습니다. 로그인 상태를 다시 확인해 주세요.",
    };
    if (httpStatus === 401) return "로그인 세션이 끝났습니다. 토큰을 다시 입력해 주세요.";
    if (httpStatus === 403) return messages[code] || "이 작업을 수행할 수 없습니다.";
    if (httpStatus === 409) return messages[code] || "기록 상태가 바뀌었습니다. 다시 불러와 주세요.";
    if (httpStatus === 410) return messages[code] || "이 기록은 만료되었습니다.";
    if (httpStatus === 422) return messages[code] || "입력값이 기록 규칙에 맞지 않습니다.";
    if (httpStatus === 429) return "요청이 잠시 제한됐습니다. 잠시 뒤 다시 시도해 주세요.";
    if (httpStatus === 503) return messages[code] || "저장 기능을 사용할 수 없습니다.";
    return messages[code] || (code === "network_error" ? "연결 오류가 발생했습니다. 저장 여부를 확인하려면 현재 상태를 다시 불러오세요." : "요청을 처리하지 못했습니다. 연결과 입력 내용을 확인해 주세요.");
  }
  async function request(path, { method = "GET", body, headers = {} } = {}) {
    const unsafe = method !== "GET" && method !== "HEAD";
    const requestHeaders = { Accept: "application/json", ...headers };
    if (body !== undefined) requestHeaders["Content-Type"] = "application/json";
    if (unsafe && csrfToken) requestHeaders["X-Rehearsal-CSRF"] = csrfToken;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    let response;
    try {
      response = await fetch(`${API}${path}`, {
        method, headers: requestHeaders, body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin", cache: "no-store", redirect: "error", signal: controller.signal,
      });
    } catch (error) {
      if (error && error.name === "AbortError") throw new Error("request_timeout");
      throw new Error("network_error");
    } finally {
      window.clearTimeout(timeout);
    }
    let payload = null;
    try { payload = await response.json(); } catch (_error) {
      // Never interpret an unexpected response as valid state or show its raw body.
      throw new Error("invalid_response");
    }
    if (!response.ok) {
      if (response.status === 401 && path !== "/login") {
        csrfToken = null;
        state = null;
        showLogin();
      }
      const error = new Error(payload && typeof payload.error === "string" ? payload.error : "request_failed");
      error.status = response.status;
      throw error;
    }
    return payload;
  }
  function fillSelect(select, rows, selected) {
    select.replaceChildren();
    for (const [value, label] of rows) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    }
    if (selected) select.value = selected;
  }
  function showLogin() {
    $("login-panel").hidden = false;
    $("logout-button").hidden = true;
    $("workspace").hidden = true;
    $("login-token").value = "";
    $("login-token").focus();
  }
  function clearTimer() {
    if (timerInterval !== null) window.clearInterval(timerInterval);
    timerInterval = null;
    timerStartedAt = null;
  }
  function loadTaskChoices(tasks) {
    const select = $("task-select");
    select.replaceChildren();
    for (const task of tasks || []) {
      if (!task || typeof task.id !== "string" || typeof task.title !== "string" || typeof task.scenario !== "string") continue;
      const option = document.createElement("option");
      option.value = task.id;
      option.textContent = task.title;
      select.appendChild(option);
    }
    updateTaskPreview();
  }
  function activeTask() {
    return (state && Array.isArray(state.tasks) ? state.tasks : []).find((task) => task.id === $("task-select").value) || null;
  }
  function updateTaskPreview() {
    const task = activeTask();
    setText("task-preview", task ? `${task.title} 사례를 선택했습니다. 사례 내용은 ‘사례 공개하고 과제 시작’을 누른 뒤 표시됩니다.` : "가상 사례를 불러오지 못했습니다.");
    updateConsentButton();
  }
  function renderRecords(records) {
    const list = $("record-list");
    list.replaceChildren();
    if (!Array.isArray(records) || records.length === 0) {
      const item = document.createElement("li");
      item.textContent = "다시 열 수 있는 기존 기록이 없습니다.";
      list.appendChild(item);
      return;
    }
    for (const record of records) {
      if (!record || typeof record.record_id !== "string") continue;
      const item = document.createElement("li");
      const task = (state.tasks || []).find((candidate) => candidate.id === record.task_id);
      const label = document.createElement("span");
      label.textContent = `${task ? task.title : "가상 사례"} · 코드 ${record.record_id.slice(0, 8)} · 만료 ${record.expires_at || "확인 필요"} · 버전 ${record.version || "?"}`;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary-button";
      button.textContent = "기록 열기";
      button.addEventListener("click", () => withBusy(async () => {
        state = await request("/restore", { method: "POST", body: { record_id: record.record_id } });
        $("restore-record-id").value = "";
        renderState("기존 기록을 다시 열었습니다.");
      }, "global-status"));
      item.append(label, button);
      list.appendChild(item);
    }
  }
  function withBusy(action, statusId = "global-status") {
    if (busy) return;
    setBusy(true);
    Promise.resolve().then(action).catch((error) => {
      if (error.message === "request_in_progress") return;
      status(statusId, errorMessage(error.message, error.status));
      if (error.status === 409 || error.message === "request_timeout" || error.message === "network_error") $("reload-state-button").hidden = false;
    }).finally(() => setBusy(false));
  }
  function renderState(message = "") {
    if (!state || typeof state !== "object") { showLogin(); return; }
    csrfToken = typeof state.csrf_token === "string" ? state.csrf_token : null;
    $("login-panel").hidden = true;
    $("logout-button").hidden = false;
    $("workspace").hidden = false;
    $("notice-panel").hidden = false;
    $("case-panel").hidden = Boolean(state.record);
    $("restore-panel").hidden = Boolean(state.record);
    setText("notice-version", state.notice && state.notice.notice_version);
    setText("policy-version", state.notice && state.notice.policy_version);
    // The private notice is rendered as plain text and is never interpreted as markup.
    $("notice-text").textContent = state.notice && typeof state.notice.text === "string" ? state.notice.text : "안내 문구를 불러올 수 없습니다.";
    loadTaskChoices(state.tasks || []);
    renderRecords(state.available_records || []);
    const hasRecord = Boolean(state.record);
    $("session-panel").hidden = !hasRecord;
    $("observation-panel").hidden = true;
    if (!hasRecord) {
      taskStatusAfterCheckpoint = null;
      clearTimer();
      $("copy-code-button").dataset.code = "";
      $("copy-code-button").hidden = true;
      $("task-region").hidden = true;
      $("timer-panel").hidden = true;
      $("lost-timer-panel").hidden = true;
      if (message) showGlobal(message);
      return;
    }
    const record = state.record;
    lastSavedVersion = Number.isInteger(record.version) ? record.version : null;
    const task = (state.tasks || []).find((item) => item.id === record.task_id);
    setText("record-summary", `${task ? task.title : "선택 사례"} · 리허설 기록`);
    setText("expiry-line", `만료 시각: ${record.expires_at || "확인할 수 없음"} · 실제 참가자 접수: 닫힘`);
    const recordCode = record.record_id || "";
    $("copy-code-button").hidden = !recordCode;
    $("copy-code-button").dataset.code = recordCode;
    const workflow = state.workflow || {};
    const started = workflow.started === true;
    const timingKnown = Number.isInteger(workflow.timing_seconds);
    const savedObservation = record.observation && typeof record.observation === "object" ? record.observation : null;
    const savedElapsed = savedObservation && Number.isInteger(savedObservation.elapsed_seconds_monotonic)
      ? savedObservation.elapsed_seconds_monotonic : null;
    const elapsedForObservation = timingKnown ? workflow.timing_seconds : savedElapsed;
    const hasObservationTiming = timingKnown || Boolean(savedObservation);
    $("begin-task-panel").hidden = started || Boolean(savedObservation);
    $("task-region").hidden = !started;
    if (started) {
      setText("scenario-text", task ? task.scenario : "사례 내용을 확인할 수 없습니다.");
      if ($("card-frame").src === "about:blank" || !$("card-frame").src.endsWith("/rehearsal/card")) $("card-frame").src = "/rehearsal/card";
      installCardObserver();
    }
    $("timer-panel").hidden = !started || timingKnown || timerStartedAt === null;
    $("lost-timer-panel").hidden = !started || timingKnown || timerStartedAt !== null;
    if (started && !timingKnown && timerStartedAt !== null) startTicker();
    if (hasObservationTiming) {
      clearTimer();
      if (elapsedForObservation !== null) setText("timer-value", formatSeconds(elapsedForObservation));
      setText("timer-help", savedObservation
        ? "저장된 관찰 기록입니다. 사후 이해 질문은 과업 시간과 분리되어 있습니다."
        : workflow.card_displayed ? "첫 카드 표시를 관찰해 주 과업 시간을 멈췄습니다." : "카드가 표시되지 않아 기록된 실제 경과 시간입니다.");
      $("save-observation-button").disabled = false;
      const cardWasDisplayed = savedObservation ? "저장된 기록을 확인하세요" : (workflow.card_displayed ? "관찰됨" : "관찰되지 않음");
      setText("elapsed-summary", `기록된 경과 시간: ${elapsedForObservation === null ? "과업 미실행" : `${elapsedForObservation}초`} · 카드 표시: ${cardWasDisplayed}`);
    } else {
      $("save-observation-button").disabled = true;
      setText("elapsed-summary", "측정 시간을 확인한 뒤 관찰 기록을 저장할 수 있습니다.");
    }
    $("observation-panel").hidden = !hasRecord || !hasObservationTiming;
    if (savedObservation) prefillObservation(savedObservation);
    else {
      resetObservation();
      if (taskStatusAfterCheckpoint) $("task-status").value = taskStatusAfterCheckpoint;
    }
    syncNotRunRules();
    if (message) showGlobal(message);
  }
  function formatSeconds(seconds) {
    const safe = Math.max(0, Math.min(360, Number(seconds) || 0));
    return `${String(Math.floor(safe / 60)).padStart(2, "0")}:${String(safe % 60).padStart(2, "0")}`;
  }
  function elapsedNow() {
    if (timerStartedAt === null) return null;
    return Math.min(360, Math.max(0, Math.floor((performance.now() - timerStartedAt) / 1000)));
  }
  function startTicker() {
    if (timerInterval !== null) return;
    timerInterval = window.setInterval(() => {
      const elapsed = elapsedNow();
      if (elapsed === null) return;
      setText("timer-value", formatSeconds(elapsed));
      if (elapsed >= 360) checkpointTiming(360, "6분 제한에 도달해 타이머를 멈췄습니다.", "timeout");
    }, 250);
  }
  let cardObserver = null;
  function installCardObserver() {
    const frame = $("card-frame");
    frame.addEventListener("load", () => {
      try {
        const region = frame.contentDocument && frame.contentDocument.querySelector("#card-region");
        if (!region) return;
        if (cardObserver) cardObserver.disconnect();
        const observe = () => {
          if (!region.hidden) {
            if (state && (Number.isInteger(state.workflow?.timing_seconds) || state.record?.observation)) return;
            if (timerStartedAt !== null) {
              if (!cardDisplayedLocally) firstCardDisplayedElapsed = elapsedNow();
              cardDisplayedLocally = true;
              if (state && state.workflow && !Number.isInteger(state.workflow.timing_seconds)) state.workflow.card_displayed = true;
              setText("timer-help", "첫 카드 표시를 관찰해 주 과업 시간을 멈췄습니다. 이제 사후 이해 관찰을 진행할 수 있습니다.");
              const stop = () => {
                if (timerStartedAt === null) return;
                if (busy) { window.setTimeout(stop, 25); return; }
                checkpointTiming(firstCardDisplayedElapsed, "첫 카드가 표시되어 주 과업 시간을 멈췄습니다.", "card_displayed");
              };
              stop();
            } else {
              setText("session-status", "타이머 시작 시각은 복구할 수 없습니다. 실제 경과 시간을 입력하고 카드 표시 여부는 직접 확인해 선택하세요.");
            }
          }
        };
        cardObserver = new MutationObserver(observe);
        cardObserver.observe(region, { attributes: true, attributeFilter: ["hidden"] });
        observe();
      } catch (_error) {
        status("session-status", "카드 화면을 확인할 수 없습니다. 현재 상태를 다시 불러오거나 기록을 철회해 주세요.");
        $("reload-state-button").hidden = false;
      }
    }, { once: true });
  }
  async function checkpointTiming(seconds, message, reason = "manual") {
    if (busy || timingCheckpointPending || !state || !state.record || !state.workflow || !state.workflow.started || Number.isInteger(state.workflow.timing_seconds)) return;
    timingCheckpointPending = true;
    setBusy(true);
    clearTimer();
    const elapsed = Math.min(360, Math.max(0, Math.floor(seconds)));
    try {
      const cardWasDisplayed = cardDisplayedLocally || state.workflow.card_displayed === true || reason === "manual" && $("manual-card-displayed").checked;
      state = await request("/timing", { method: "POST", body: { elapsed_seconds: elapsed, card_displayed: cardWasDisplayed } });
      taskStatusAfterCheckpoint = reason === "timeout" ? "timeout" : reason === "early_stop" ? "abandoned" : cardWasDisplayed ? "completed" : null;
      renderState(message || "실제 경과 시간을 저장했습니다.");
    } catch (error) {
      status("session-status", errorMessage(error.message, error.status));
      // Do not restart an untrusted stopwatch or repeat an ambiguous checkpoint before reading state.
      $("lost-timer-panel").hidden = false;
      $("timer-panel").hidden = true;
      if (error.status === 409 || error.message === "request_timeout" || error.message === "network_error") {
        $("reload-state-button").hidden = false;
        $("manual-timing-form").dataset.needsStateRefresh = "true";
      }
    } finally {
      timingCheckpointPending = false;
      setBusy(false);
    }
  }
  function resetObservation() {
    for (const [id, options] of Object.entries(SELECTS)) fillSelect($(id), options);
    $("task-status").value = "not_run";
    $("stuck-count").value = "";
    $("recording-status").value = "not_recorded";
  }
  function clearConsentInputs() {
    $("adult-consent").checked = false;
    $("participation-consent").checked = false;
    $("recording-consent").checked = false;
    updateConsentButton();
  }
  function clearSessionDrafts() {
    clearConsentInputs();
    $("restore-record-id").value = "";
    $("manual-seconds").value = "";
    $("manual-card-displayed").checked = false;
    $("manual-timing-form").dataset.needsStateRefresh = "false";
    resetObservation();
    $("copy-code-button").dataset.code = "";
  }
  const OBSERVATION_IDS = {
    task_status: "task-status", help_requested_during_task: "help-requested", content_help_during_task: "content-help",
    technical_issue_during_task: "technical-issue", stuck_event_count_during_task: "stuck-count",
    next_action_comprehension_post_task: "next-action", source_link_date_location_comprehension_post_task: "source-understanding",
    preparation_list_meaning_comprehension_post_task: "list-understanding", card_limits_comprehension_post_task: "limits-understanding",
    critical_misunderstanding_observed: "critical-misunderstanding", critical_misunderstanding_phase: "critical-phase",
    sensitive_data_handling_status: "sensitive-data", recording_status: "recording-status",
  };
  function prefillObservation(observation) {
    for (const [field, id] of Object.entries(OBSERVATION_IDS)) {
      if (observation[field] === null || observation[field] === undefined) $(id).value = "";
      else $(id).value = String(observation[field]);
    }
  }
  function syncNotRunRules() {
    const notRun = $("task-status").value === "not_run";
    for (const id of ["help-requested", "content-help", "technical-issue", "next-action", "source-understanding", "list-understanding", "limits-understanding", "critical-misunderstanding"]) {
      const control = $(id);
      if (notRun) control.value = id === "critical-misunderstanding" ? "not_observed" : "not_observed";
      control.disabled = busy || notRun;
    }
    $("stuck-count").disabled = busy || notRun;
    if (notRun) {
      $("critical-phase").value = "unknown";
      $("stuck-count").value = "";
    }
  }
  function collectObservation() {
    const workflow = state.workflow || {};
    const notRun = $("task-status").value === "not_run";
      const elapsed = notRun ? null : Number.isInteger(workflow.timing_seconds)
        ? workflow.timing_seconds
        : state.record && state.record.observation ? state.record.observation.elapsed_seconds_monotonic : null;
    const observation = {};
    for (const [field, id] of Object.entries(OBSERVATION_IDS)) {
      if (field === "stuck_event_count_during_task") {
        observation[field] = notRun || $(id).value === "" ? null : Number($(id).value);
      } else if (field === "recording_status") {
        observation[field] = "not_recorded";
      } else {
        observation[field] = $(id).value;
      }
    }
    observation.elapsed_seconds_monotonic = elapsed;
    if (notRun) {
      observation.help_requested_during_task = "not_observed";
      observation.content_help_during_task = "not_observed";
      observation.technical_issue_during_task = "not_observed";
      observation.stuck_event_count_during_task = null;
      observation.next_action_comprehension_post_task = "not_observed";
      observation.source_link_date_location_comprehension_post_task = "not_observed";
      observation.preparation_list_meaning_comprehension_post_task = "not_observed";
      observation.card_limits_comprehension_post_task = "not_observed";
      observation.critical_misunderstanding_observed = "not_observed";
      observation.critical_misunderstanding_phase = "unknown";
    }
    if (observation.critical_misunderstanding_observed !== "yes") observation.critical_misunderstanding_phase = "unknown";
    return observation;
  }
  async function bootstrap() {
    try {
      state = await request("/state");
      renderState();
    } catch (error) {
      if (error.status === 401) showLogin();
      else { showLogin(); showGlobal(errorMessage(error.message, error.status)); }
    }
  }

  for (const [id, rows] of Object.entries(SELECTS)) fillSelect($(id), rows);
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    const tokenInput = $("login-token");
    const token = tokenInput.value;
    if (!token) return;
    setBusy(true);
    request("/login", { method: "POST", body: { token } }).then((next) => {
      tokenInput.value = "";
      state = next;
      renderState("로그인했습니다.");
    }).catch((error) => {
      tokenInput.value = "";
      status("global-status", errorMessage(error.message, error.status));
      tokenInput.focus();
    }).finally(() => setBusy(false));
  });
  $("logout-button").addEventListener("click", () => withBusy(async () => {
    await request("/logout", { method: "POST", body: {} });
    state = null; csrfToken = null; cardDisplayedLocally = false; clearTimer();
    clearSessionDrafts();
    $("card-frame").src = "about:blank";
    $("notice-text").textContent = "";
    $("record-list").replaceChildren();
    showLogin(); showGlobal("로그아웃했습니다.");
  }));
  $("task-select").addEventListener("change", updateTaskPreview);
  ["adult-consent", "participation-consent", "recording-consent"].forEach((id) => $(id).addEventListener("change", updateConsentButton));
  $("accept-button").addEventListener("click", () => withBusy(async () => {
    const selectedTask = activeTask();
    if (!selectedTask) return;
    const body = {
      task_id: selectedTask.id,
      consent: {
        adult_confirmed: $("adult-consent").checked,
        participation_agreed: $("participation-consent").checked,
        observation_recording_agreed: $("recording-consent").checked,
        notice_version: state.notice.notice_version,
        policy_version: state.notice.policy_version,
      },
    };
    state = await request("/consent", { method: "POST", body });
    if (state.record) renderState("동의가 기록됐습니다. 사례 시작 버튼을 누르면 타이머가 시작됩니다.");
    else renderState("동의를 확인하지 않았습니다. 거절은 저장되지 않았습니다.");
  }, "case-status"));
  $("decline-button").addEventListener("click", () => withBusy(async () => {
    state = await request("/consent", {
      method: "POST",
      body: { task_id: $("task-select").value, consent: {
        adult_confirmed: false, participation_agreed: false, observation_recording_agreed: false,
        notice_version: state.notice.notice_version, policy_version: state.notice.policy_version,
      } },
    });
    clearConsentInputs();
    renderState("동의하지 않았습니다. 거절에 대한 기록은 생성되지 않습니다.");
  }, "case-status"));
  $("begin-task-button").addEventListener("click", () => withBusy(async () => {
    state = await request("/start", { method: "POST", body: {} });
    cardDisplayedLocally = false;
    firstCardDisplayedElapsed = null;
    $("manual-card-displayed").checked = false;
    $("card-frame").src = "about:blank";
    timerStartedAt = performance.now();
    renderState("사례를 공개했습니다. 이 순간부터 경과 시간을 측정합니다.");
  }, "session-status"));
  $("manual-timing-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if ($("manual-timing-form").dataset.needsStateRefresh === "true") {
      status("session-status", "먼저 현재 상태를 다시 불러와 요청 결과를 확인하세요.");
      return;
    }
    const value = Number($("manual-seconds").value);
    if (!Number.isInteger(value) || value < 0 || value > 360) { status("session-status", "실제로 측정한 0–360초 정수를 입력하세요."); return; }
    checkpointTiming(value, "입력한 실측 경과 시간을 저장했습니다.");
  });
  $("restart-task-button").addEventListener("click", () => withBusy(async () => {
    state = await request("/restart", { method: "POST", body: {} });
    cardDisplayedLocally = false;
    firstCardDisplayedElapsed = null;
    $("manual-card-displayed").checked = false;
    $("card-frame").src = "about:blank";
    timerStartedAt = performance.now();
    renderState("과제를 명시적으로 다시 시작했습니다. 이 시점부터 경과 시간을 측정합니다.");
  }, "session-status"));
  $("restore-code-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const recordId = $("restore-record-id").value.trim();
    if (!/^[0-9a-f-]{36}$/i.test(recordId)) {
      status("global-status", "기록 UUID 36자를 확인해 주세요.");
      return;
    }
    withBusy(async () => {
      state = await request("/restore", { method: "POST", body: { record_id: recordId } });
      $("restore-record-id").value = "";
      renderState("기존 기록을 다시 열었습니다. 새 동의 기록은 만들지 않았습니다.");
    });
  });
  $("reload-state-button").addEventListener("click", () => withBusy(async () => {
    state = await request("/state");
    $("reload-state-button").hidden = true;
    $("manual-timing-form").dataset.needsStateRefresh = "false";
    renderState("현재 서버 상태를 다시 불러왔습니다.");
  }));
  $("stop-task-button").addEventListener("click", () => {
    const elapsed = elapsedNow();
    if (elapsed === null) return;
    checkpointTiming(elapsed, "카드 표시 전 과제를 중단하고 실제 경과 시간을 저장했습니다.", "early_stop");
  });
  $("observation-form").addEventListener("change", (event) => {
    if (event.target.id === "task-status") syncNotRunRules();
    if (event.target.id === "critical-misunderstanding" && event.target.value !== "yes") $("critical-phase").value = "unknown";
  });
  $("observation-form").addEventListener("submit", (event) => {
    event.preventDefault();
    withBusy(async () => {
      if (!state || !state.record || !Number.isInteger(lastSavedVersion)) return;
      const observation = collectObservation();
      if (observation.task_status === "timeout" && observation.elapsed_seconds_monotonic !== 360) {
        status("observation-status", "‘6분 종료’는 실제 경과 시간이 360초일 때만 선택할 수 있습니다."); return;
      }
      if (observation.task_status !== "not_run" && (observation.elapsed_seconds_monotonic === null || observation.stuck_event_count_during_task === null)) {
        status("observation-status", "실행한 과업은 경과 시간과 막힘 횟수를 입력해야 합니다."); return;
      }
      state = await request("/observations", { method: "POST", body: { expected_version: lastSavedVersion, observation } });
      renderState("코드형 관찰 기록을 저장했습니다.");
      status("observation-status", "저장했습니다. 입력한 시간은 진행자가 보고한 실제 경과 시간입니다.");
    }, "observation-status");
  });
  $("withdraw-button").addEventListener("click", () => {
    if (!window.confirm("이 리허설 기록을 철회하고 저장 데이터에서 삭제할까요? 삭제하면 되돌릴 수 없습니다.")) return;
    withBusy(async () => {
      state = await request("/withdraw", { method: "POST", body: {} });
      clearTimer(); cardDisplayedLocally = false; $("card-frame").src = "about:blank";
      firstCardDisplayedElapsed = null;
      clearSessionDrafts();
      renderState("기록을 철회하고 삭제했습니다.");
    }, "observation-status");
  });
  $("copy-code-button").addEventListener("click", async () => {
    const code = $("copy-code-button").dataset.code;
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
      status("session-status", "기록 코드를 복사했습니다.");
    } catch (_error) {
      status("session-status", `기록 코드: ${code}`);
    }
  });
  let frameResizeObserver = null;
  $("card-frame").addEventListener("load", () => {
    try {
      const frame = $("card-frame");
      const doc = frame.contentDocument;
      if (!doc || !doc.body) return;
      const resize = () => { frame.height = String(Math.max(420, doc.documentElement.scrollHeight, doc.body.scrollHeight)); };
      if (frameResizeObserver) frameResizeObserver.disconnect();
      frameResizeObserver = new ResizeObserver(resize);
      frameResizeObserver.observe(doc.documentElement);
      if (doc.body !== doc.documentElement) frameResizeObserver.observe(doc.body);
      resize();
    } catch (_error) { /* Browser policy keeps an inaccessible frame contained at its default height. */ }
  });
  bootstrap();
})();
