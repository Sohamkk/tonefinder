/* Tonefinder front end */
(function () {
  "use strict";

  var el = function (id) { return document.getElementById(id); };
  var state = { user: null, file: null, blob: null, busy: false, last: null, mode: "login", nonce: 0 };

  /* ---------------- api helper ---------------- */
  async function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || "GET", credentials: "same-origin" };
    if (opts.body instanceof FormData) {
      init.body = opts.body;
    } else if (opts.body) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(opts.body);
    }
    var res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      return { ok: false, error: "Can't reach the server. Check that it's running." };
    }
    var data;
    try {
      data = await res.json();
    } catch (e) {
      return { ok: false, error: "The server sent an unexpected response (" + res.status + ")." };
    }
    if (data.ok === undefined) data.ok = res.ok;
    data.status = res.status;
    return data;
  }

  /* ---------------- theme ---------------- */
  el("themeBtn").addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme");
    var dark = cur ? cur === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.setAttribute("data-theme", dark ? "light" : "dark");
  });

  /* ---------------- auth ---------------- */
  function openAuth(mode) {
    state.mode = mode || "login";
    var signup = state.mode === "signup";
    el("authTitle").textContent = signup ? "Create an account" : "Sign in";
    el("authSub").textContent = signup
      ? "Free plan, no card. Three songs per photo, five photos a day."
      : "Accounts keep your plan and your recent reads.";
    el("nameField").hidden = !signup;
    el("authSubmit").textContent = signup ? "Create account" : "Sign in";
    el("swapText").textContent = signup ? "Already have an account?" : "New here?";
    el("swapBtn").textContent = signup ? "Sign in" : "Create an account";
    el("authErr").hidden = true;
    el("authModal").hidden = false;
    el("authEmail").focus();
  }
  function closeAuth() { el("authModal").hidden = true; }

  el("signInBtn").addEventListener("click", function () { openAuth("login"); });
  el("closeAuth").addEventListener("click", closeAuth);
  el("swapBtn").addEventListener("click", function () { openAuth(state.mode === "signup" ? "login" : "signup"); });
  el("authModal").addEventListener("click", function (e) { if (e.target === this) closeAuth(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeAuth(); });

  ["authEmail", "authPass", "authName"].forEach(function (id) {
    el(id).addEventListener("keydown", function (e) { if (e.key === "Enter") submitAuth(); });
  });
  el("authSubmit").addEventListener("click", submitAuth);

  async function submitAuth() {
    var btn = el("authSubmit");
    btn.disabled = true;
    var body = { email: el("authEmail").value.trim(), password: el("authPass").value };
    if (state.mode === "signup") body.name = el("authName").value.trim();
    var path = state.mode === "signup" ? "/api/auth/register" : "/api/auth/login";
    var r = await api(path, { method: "POST", body: body });
    btn.disabled = false;
    if (!r.ok) {
      el("authErr").textContent = r.error || "That didn't work.";
      el("authErr").hidden = false;
      return;
    }
    state.user = r.user;
    closeAuth();
    paintAccount();
    if (state.file) showStatus("Signed in. Press <strong>Find songs</strong> when you're ready.");
  }

  el("logoutBtn").addEventListener("click", async function () {
    await api("/api/auth/logout", { method: "POST" });
    state.user = null;
    paintAccount();
    el("acctMenu").hidden = true;
  });

  el("acctBtn").addEventListener("click", function () {
    el("acctMenu").hidden = !el("acctMenu").hidden;
  });
  document.addEventListener("click", function (e) {
    if (!el("acct").contains(e.target)) el("acctMenu").hidden = true;
  });
  el("upgradeLink").addEventListener("click", function () {
    el("acctMenu").hidden = true;
    el("plans").scrollIntoView({ behavior: "smooth" });
  });
  el("historyLink").addEventListener("click", function () {
    el("acctMenu").hidden = true;
    loadHistory();
  });

  function isPro() {
    return !!(state.user && state.user.limits && state.user.limits.filters);
  }

  function paintAccount() {
    var signed = !!state.user;
    el("signInBtn").hidden = signed;
    el("acct").hidden = !signed;
    el("planBadge").hidden = !signed;
    if (signed) {
      el("acctBtn").textContent = state.user.name;
      el("planBadge").textContent = state.user.planName;
    }
    el("veil").hidden = isPro();
    Array.prototype.forEach.call(document.querySelectorAll("#count option[data-pro]"), function (o) {
      o.disabled = !isPro();
    });
    if (!isPro() && Number(el("count").value) > 3) el("count").value = "3";

    document.querySelectorAll(".plan").forEach(function (p) {
      p.classList.toggle("current", signed && p.dataset.plan === state.user.plan);
    });
    paintQuota();
  }

  function paintQuota() {
    var q = el("quota");
    if (!state.user) { q.textContent = "Sign in to run a read."; return; }
    var lim = state.user.limits.dailyPhotos;
    q.textContent = lim ? (state.usage || 0) + " of " + lim + " photos used today" : "Unlimited photos on " + state.user.planName;
  }

  el("veil").addEventListener("click", function () {
    if (!state.user) return openAuth("signup");
    el("plans").scrollIntoView({ behavior: "smooth" });
  });

  /* ---------------- colour extraction ---------------- */
  function extractColors(img) {
    var c = document.createElement("canvas"), n = 60;
    c.width = n; c.height = n;
    var ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(img, 0, 0, n, n);
    var d;
    try { d = ctx.getImageData(0, 0, n, n).data; } catch (e) { return []; }
    var buckets = {};
    for (var i = 0; i < d.length; i += 4) {
      if (d[i + 3] < 128) continue;
      var key = (d[i] >> 5) + "-" + (d[i + 1] >> 5) + "-" + (d[i + 2] >> 5);
      var bk = buckets[key] || (buckets[key] = { r: 0, g: 0, b: 0, n: 0 });
      bk.r += d[i]; bk.g += d[i + 1]; bk.b += d[i + 2]; bk.n++;
    }
    var list = Object.keys(buckets).map(function (k) {
      var bk = buckets[k], r = bk.r / bk.n, g = bk.g / bk.n, b = bk.b / bk.n;
      var mx = Math.max(r, g, b), mn = Math.min(r, g, b);
      return { n: bk.n, hex: hex(r, g, b), sat: mx === 0 ? 0 : (mx - mn) / mx };
    });
    list.sort(function (a, b) { return (b.n * (0.6 + b.sat)) - (a.n * (0.6 + a.sat)); });
    return list.slice(0, 5).map(function (x) { return x.hex; });
  }
  function hex(r, g, b) {
    return "#" + [r, g, b].map(function (v) {
      var s = Math.max(0, Math.min(255, Math.round(v))).toString(16);
      return s.length < 2 ? "0" + s : s;
    }).join("");
  }
  function paintColors(colors) {
    var strip = el("strip");
    strip.innerHTML = "";
    colors.forEach(function (c, i) {
      var s = document.createElement("span");
      s.style.background = c;
      strip.appendChild(s);
      document.documentElement.style.setProperty("--swatch-" + (i + 1), c);
    });
    var lead = colors[2] || colors[0];
    if (lead) {
      document.documentElement.style.setProperty("--swatch-3", lead);
      el("brandDot").style.background = lead;
    }
  }

  /* ---------------- upload ---------------- */
  var drop = el("drop");
  ["dragenter", "dragover"].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("drag"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("drag"); });
  });
  drop.addEventListener("drop", function (e) {
    var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) takeFile(f);
  });
  el("file").addEventListener("change", function () {
    if (this.files && this.files[0]) takeFile(this.files[0]);
  });
  el("clearBtn").addEventListener("click", function () {
    state.file = null; state.blob = null;
    el("shot").hidden = true;
    drop.hidden = false;
    el("file").value = "";
  });

  /* Resize in the browser so the upload is small and the server never
     has to run Pillow. Long edge 1280px, JPEG quality 0.85. */
  function shrink(img) {
    return new Promise(function (resolve) {
      var max = 1280;
      var scale = Math.min(1, max / Math.max(img.naturalWidth, img.naturalHeight));
      var w = Math.round(img.naturalWidth * scale), h = Math.round(img.naturalHeight * scale);
      var c = document.createElement("canvas");
      c.width = w; c.height = h;
      c.getContext("2d").drawImage(img, 0, 0, w, h);
      c.toBlob(function (b) { resolve(b); }, "image/jpeg", 0.85);
    });
  }

  function takeFile(f) {
    if (!/^image\/(jpeg|png|webp)$/.test(f.type)) {
      return showStatus("That file type won't work. Use a JPEG, PNG or WebP.", true);
    }
    state.file = f;
    state.nonce = 0;
    var url = URL.createObjectURL(f);
    var img = el("preview");
    img.onload = async function () {
      paintColors(extractColors(img));
      state.blob = await shrink(img);
      URL.revokeObjectURL(url);
    };
    img.src = url;
    el("shotName").textContent = f.name.length > 30 ? f.name.slice(0, 28) + "…" : f.name;
    el("shot").hidden = false;
    drop.hidden = true;
    showStatus(state.user
      ? "Photo loaded. Press <strong>Find songs</strong> when you're ready."
      : "Photo loaded. <strong>Sign in</strong> and Tonefinder will read it.");
  }

  /* ---------------- status ---------------- */
  function showStatus(html, bad, thinking) {
    el("results").innerHTML = '<div class="status' + (bad ? " bad" : "") + '">' +
      (thinking ? '<span class="pulse"></span>' : "") + html + "</div>";
  }

  /* ---------------- analyze ---------------- */
  el("goBtn").addEventListener("click", run);
  el("energy").addEventListener("input", function () {
    var v = Number(this.value);
    el("energyVal").textContent = v === 0 ? "— let the photo decide" : "— at least " + v + " / 100";
  });

  async function run() {
    if (state.busy) return;
    if (!state.user) return openAuth("signup");
    if (!state.file) return showStatus("Pick a photo first — drop one in the frame on the left.", true);
    if (!state.blob) return showStatus("Still preparing the photo. Try again in a second.");

    state.busy = true;
    el("goBtn").disabled = true;
    el("goBtn").textContent = "Reading the photo…";
    showStatus("<strong>Reading the photo.</strong><br>Working out the setting, the light and the mood before picking anything, then checking each song against Spotify. Give it a few seconds.", false, true);

    var fd = new FormData();
    fd.append("photo", state.blob, "photo.jpg");
    fd.append("count", el("count").value);
    fd.append("nonce", String(state.nonce));
    if (isPro()) {
      fd.append("lang", el("lang").value);
      fd.append("era", el("era").value);
      fd.append("steer", el("steer").value.trim());
      fd.append("energy", el("energy").value);
    }

    var r = await api("/api/analyze", { method: "POST", body: fd });

    state.busy = false;
    el("goBtn").disabled = false;
    el("goBtn").textContent = "Find songs";

    if (!r.ok) {
      if (r.status === 401) { openAuth("login"); return; }
      showStatus("<strong>The read didn't finish.</strong><br>" + escapeHtml(r.error || "") +
        (r.upgrade ? '<br><br><button class="cta ui" id="toPlans" style="width:auto">See plans</button>' : ""), true);
      var tp = el("toPlans");
      if (tp) tp.addEventListener("click", function () { el("plans").scrollIntoView({ behavior: "smooth" }); });
      return;
    }

    state.usage = r.usage;
    paintQuota();
    state.last = r.result;
    render(r.result);
    if (isPro()) loadCaption(r.result);
  }

  /* ---------------- render ---------------- */
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
    });
  }

  function render(d) {
    var songs = Array.isArray(d.songs) ? d.songs : [];
    var energy = Math.max(0, Math.min(100, Number(d.energy) || 0));
    var moods = Array.isArray(d.moods) ? d.moods.slice(0, 6) : [];

    var html = '<div class="readout">' +
      '<p class="scene">' + escapeHtml(d.scene || "A photo.") + "</p>" +
      '<div class="tags">' +
        (d.timeOfDay ? '<span class="tag">' + escapeHtml(d.timeOfDay) + "</span>" : "") +
        (d.palette ? '<span class="tag">' + escapeHtml(d.palette) + "</span>" : "") +
        moods.map(function (m) { return '<span class="tag">' + escapeHtml(m) + "</span>"; }).join("") +
      "</div>" +
      '<div class="meter" title="Energy ' + energy + ' of 100"><i style="width:' + energy + '%"></i></div>' +
      (d.providerLabel ? '<p class="mismatch">Read by the ' + escapeHtml(d.providerLabel) + '.</p>' : "") +
      "</div><div id=\"capSlot\"></div><ul class=\"tracks\">";

    songs.forEach(function (s) {
      var q = encodeURIComponent((s.title || "") + " " + (s.artist || ""));
      var mismatch = s.matchedTitle && s.title &&
        s.matchedTitle.toLowerCase().indexOf(String(s.title).toLowerCase().slice(0, 12)) === -1;
      html += '<li class="track"><div class="top">' +
        '<h3 class="name">' + escapeHtml(s.title) + "</h3>" +
        '<span class="by">' + escapeHtml(s.artist) + "</span>" +
        (s.year ? '<span class="yr">' + escapeHtml(s.year) + (s.language ? " · " + escapeHtml(s.language) : "") + "</span>" : "") +
        "</div>" +
        '<p class="why">' + escapeHtml(s.why || "") + "</p>" +
        (s.id
          ? '<div class="player"><iframe loading="lazy" allow="encrypted-media" title="Spotify player for ' +
            escapeHtml(s.title) + '" src="https://open.spotify.com/embed/track/' + escapeHtml(s.id) +
            '?utm_source=generator"></iframe></div>' +
            (mismatch ? '<p class="mismatch">Closest match on Spotify: ' + escapeHtml(s.matchedTitle) + " — " + escapeHtml(s.matchedArtist) + "</p>" : "")
          : '<p class="mismatch">Not found on Spotify — search it below.</p>') +
        '<div class="acts">' +
          '<a class="chip primary ui" target="_blank" rel="noopener" href="' +
            (s.url || "https://open.spotify.com/search/" + q) + '">Open in Spotify</a>' +
          '<a class="chip ui" target="_blank" rel="noopener" href="https://music.youtube.com/search?q=' + q + '">YouTube Music</a>' +
        "</div></li>";
    });

    html += "</ul><div style=\"display:flex;gap:10px;margin-top:20px;flex-wrap:wrap\">" +
      '<button class="cta ghost ui" id="againBtn" style="width:auto">Suggest a different set</button>' +
      (isPro() ? '<button class="cta ghost ui" id="saveBtn" style="width:auto">Download the list</button>' : "") +
      "</div>";

    el("results").innerHTML = html;
    el("againBtn").addEventListener("click", function () { state.nonce += 1; run(); });
    var save = el("saveBtn");
    if (save) save.addEventListener("click", function () { downloadList(d); });
  }

  async function loadCaption(d) {
    var slot = el("capSlot");
    if (!slot) return;
    slot.innerHTML = '<div class="caption-box"><h4 class="ui">Caption</h4><p><span class="pulse"></span>Writing one…</p></div>';
    var r = await api("/api/caption", { method: "POST", body: { scene: d.scene, moods: d.moods } });
    if (!r.ok) { slot.innerHTML = ""; return; }
    var tags = (r.hashtags || []).map(function (t) { return "#" + String(t).replace(/^#/, ""); });
    slot.innerHTML = '<div class="caption-box"><h4 class="ui">Caption</h4><p>' +
      escapeHtml(r.caption) + '</p><p class="hash">' + escapeHtml(tags.join(" ")) + "</p></div>";
  }

  function downloadList(d) {
    var lines = ["Tonefinder — songs for your photo", ""];
    if (d.scene) lines.push("Photo: " + d.scene);
    if (d.moods) lines.push("Mood: " + [].concat(d.moods).join(", "));
    lines.push("");
    (d.songs || []).forEach(function (s, i) {
      lines.push((i + 1) + ". " + s.title + " — " + s.artist + (s.year ? " (" + s.year + ")" : ""));
      if (s.why) lines.push("   " + s.why);
      if (s.url) lines.push("   " + s.url);
    });
    var blob = new Blob([lines.join("\n")], { type: "text/plain" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "tonefinder-songs.txt";
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
  }

  async function loadHistory() {
    var r = await api("/api/history");
    if (!r.ok || !r.items.length) return showStatus("<strong>No saved reads yet.</strong><br>Run one and it shows up here.");
    var html = '<div class="status"><strong>Your recent photos</strong></div><ul class="tracks" style="margin-top:14px">';
    r.items.forEach(function (it) {
      var titles = (it.result.songs || []).map(function (s) { return s.title; }).join(" · ");
      html += '<li class="track"><p class="why" style="margin:0">' + escapeHtml(it.scene || "") +
        '</p><p class="mismatch">' + escapeHtml(titles) + "</p></li>";
    });
    el("results").innerHTML = html + "</ul>";
  }

  /* ---------------- payments ---------------- */
  document.querySelectorAll("[data-buy]").forEach(function (b) {
    b.addEventListener("click", function () { buy(b.dataset.buy, b); });
  });

  async function buy(planId, btn) {
    if (!state.user) return openAuth("signup");
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Opening checkout…";
    var r = await api("/api/subscribe", { method: "POST", body: { plan: planId } });
    btn.disabled = false;
    btn.textContent = label;

    if (!r.ok) { note(r.error || "Checkout couldn't start."); return; }
    if (!r.paid) { state.user = r.user; paintAccount(); note("You're on the Free plan."); return; }
    if (typeof Razorpay === "undefined") { note("Razorpay's checkout script didn't load. Check your connection and reload."); return; }

    var rzp = new Razorpay({
      key: r.key_id,
      amount: r.amount,
      currency: r.currency,
      name: "Tonefinder",
      description: r.plan_name + " plan",
      order_id: r.order_id,
      prefill: r.prefill,
      theme: { color: "#2F4B7C" },
      handler: async function (resp) {
        note("Checking the payment…");
        var v = await api("/api/verify-payment", { method: "POST", body: resp });
        if (!v.ok) { note(v.error); return; }
        state.user = v.user;
        paintAccount();
        note("Payment verified. " + v.user.planName + " is active.");
      },
      modal: { ondismiss: function () { note("Checkout closed — nothing was charged."); } }
    });
    rzp.on("payment.failed", function (e) {
      note("Payment failed: " + ((e.error && e.error.description) || "unknown reason") + ". Your plan hasn't changed.");
    });
    rzp.open();
  }

  function note(msg) { el("payNote").textContent = msg || ""; }

  /* ---------------- boot ---------------- */
  (async function () {
    var r = await api("/api/me");
    if (r.ok) {
      state.user = r.user;
      state.usage = r.usage || 0;
    }
    paintAccount();
    if (!state.user) showStatus("<strong>Sign in to start.</strong><br>The free plan gives you three songs a photo, five photos a day, no card needed.");
  })();
})();
