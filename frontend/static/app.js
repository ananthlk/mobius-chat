(function () {
  const API_BASE = (window.API_BASE && window.API_BASE.startsWith("http")) ? window.API_BASE : "http://localhost:8000";
  const messagesEl = document.getElementById("messages");
  const inputEl = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const drawer = document.getElementById("drawer");
  const drawerOverlay = document.getElementById("drawerOverlay");
  const hamburger = document.getElementById("hamburger");
  const drawerClose = document.getElementById("drawerClose");

  function openDrawer() {
    drawer.classList.add("open");
    drawerOverlay.classList.add("open");
    loadChatConfig();
  }
  function closeDrawer() {
    drawer.classList.remove("open");
    drawerOverlay.classList.remove("open");
  }
  hamburger.addEventListener("click", openDrawer);
  drawerClose.addEventListener("click", closeDrawer);
  drawerOverlay.addEventListener("click", closeDrawer);

  function loadChatConfig() {
    fetch(API_BASE + "/chat/config")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var p = data.prompts || {};
        document.getElementById("promptFirstGenSystem").textContent = p.first_gen_system || "—";
        document.getElementById("promptFirstGenUser").textContent = p.first_gen_user_template || "—";
        var llm = data.llm || {};
        document.getElementById("configLlm").textContent =
          "Provider: " + (llm.provider || "—") + ", Model: " + (llm.model || "—") +
          (llm.temperature != null ? ", Temp: " + llm.temperature : "");
        var parser = data.parser || {};
        document.getElementById("configParser").textContent =
          "Patient keywords: " + (parser.patient_keywords && parser.patient_keywords.length ? parser.patient_keywords.join(", ") : "—");
      })
      .catch(function () {
        document.getElementById("promptFirstGenSystem").textContent = "Failed to load config.";
        document.getElementById("configLlm").textContent = "Failed to load config.";
      });
  }

  function append(cls, text) {
    const div = document.createElement("div");
    div.className = "msg " + cls;
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function pollResponse(correlationId, onThinking) {
    return new Promise(function (resolve, reject) {
      const maxAttempts = 60;
      let attempts = 0;
      function poll() {
        fetch(API_BASE + "/chat/response/" + correlationId)
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (data.thinking_log && data.thinking_log.length > 0 && onThinking) {
              data.thinking_log.forEach(function (line) {
                onThinking(line);
              });
              onThinking = null;
            }
            if (data.status === "completed") {
              resolve(data);
              return;
            }
            attempts++;
            if (attempts >= maxAttempts) {
              reject(new Error("Timeout waiting for response"));
              return;
            }
            setTimeout(poll, 500);
          })
          .catch(reject);
      }
      poll();
    });
  }

  sendBtn.addEventListener("click", function () {
    const message = (inputEl.value || "").trim();
    if (!message) return;
    append("user", "You: " + message);
    inputEl.value = "";
    sendBtn.disabled = true;

    fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: message }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        const correlationId = data.correlation_id;
        append("thinking", "Request sent to Redis queue. Worker planning…");
        const thinkingContainer = document.createElement("div");
        thinkingContainer.className = "msg thinking";
        messagesEl.appendChild(thinkingContainer);
        function addThinking(line) {
          thinkingContainer.textContent = (thinkingContainer.textContent || "") + line + "\n";
          messagesEl.scrollTop = messagesEl.scrollHeight;
        }
        return pollResponse(correlationId, addThinking);
      })
      .then(function (data) {
        const thinkingEl = messagesEl.querySelector(".msg.thinking:last-of-type");
        if (thinkingEl) thinkingEl.textContent = (data.thinking_log || []).join("\n") || "Planning done.";
        append("thinking", "Response back (from worker via Redis queue):");
        append("assistant", data.message || "(No message)");
        if (data.response_source === "llm" && data.model_used) {
          append("thinking", "Model: " + data.model_used);
        }
        if (data.response_source === "stub") {
          if (data.llm_error) {
            append("error", "LLM failed (stub used): " + data.llm_error);
          } else {
            append("error", "Response from stub (LLM did not return an answer). With Redis, run the worker separately: python -m app.worker");
          }
        }
      })
      .catch(function (err) {
        append("error", "Error: " + (err.message || String(err)));
      })
      .finally(function () {
        sendBtn.disabled = false;
      });
  });
})();
