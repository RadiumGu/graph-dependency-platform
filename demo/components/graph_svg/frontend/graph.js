/*
 * graph.js —— dagre 分层 + 自绘 SVG 的依赖图，Streamlit Components v2 内联组件。
 *
 * ## 为什么自绘而不是再包一个图库
 *
 * st-link-analysis（Cytoscape 封装）把 Cytoscape 的 `min-zoomed-font-size` 硬编码
 * 成 10，而它既不暴露 font-size 也不透传 cy 实例：fit 后缩放落在 0.625 以下时
 * **节点标签被整体隐藏**，一张依赖图连节点叫什么都读不出来，且在封装内无法绕开。
 * 自绘的第一条好处就是这类阈值不存在——文字是我们自己画的 <text>。
 *
 * ## 布局仍然用 dagre，不用 d3-hierarchy 也不用 d3-dag
 *
 * - d3-hierarchy（tree/cluster）硬性要求单根、无环、每节点单父，依赖图有共享依赖
 *   （多父）也可能有环，数据模型就不匹配；硬套只能把共享依赖**克隆**成多个节点，
 *   那样「dynamodb 被 5 个服务依赖」会散成 5 个点，看不出它是关键依赖。
 * - d3-dag 有更好的去交叉，但要求**调用方自己**把成环的边标成 reversed，且不处理自环。
 * - dagre 自带 acyclic 预处理（DFS / greedy FAS）**自动断环**，对「可能有环」的
 *   真实依赖图是更安全的默认。这是从 Cytoscape 换过来时要保住的能力，不是要丢的。
 *
 * dagre 以 UMD 全局载入（同目录 dagre.min.js 由 Python 侧拼进 js），
 * 不引 d3：布局是 dagre 算的，画和交互用原生 DOM 就够，少一个依赖少一处版本耦合。
 */

export default function (component) {
  const { data, setStateValue, setTriggerValue, parentElement } = component;

  const root =
    parentElement instanceof ShadowRoot ? parentElement : parentElement;
  const mount = root.querySelector("div") || root;

  if (!data || !Array.isArray(data.nodes) || data.nodes.length === 0) {
    mount.innerHTML =
      '<div class="gs-empty">没有可画的节点 —— 先选一个起点服务并点「从这里开始」。</div>';
    return;
  }

  // CSS 由 Python 侧读文件传进来，注入到 shadow root（isolate_styles=true 时
  // 外部样式表进不来，必须自己塞）。
  if (data._css && !root.querySelector("style[data-gs]")) {
    const st = document.createElement("style");
    st.setAttribute("data-gs", "1");
    st.textContent = data._css;
    root.appendChild(st);
  }

  const NODE_H = 30;
  const CHAR_W = 7.4; // 13px sans-serif 的中英混排近似宽度
  const PAD_X = 14;
  const MIN_W = 78;
  const MAX_W = 250;

  const labelOf = (n) => n.label || n.id;
  const widthOf = (n) =>
    Math.max(MIN_W, Math.min(MAX_W, labelOf(n).length * CHAR_W + PAD_X * 2));

  // ── 布局：dagre ────────────────────────────────────────────────────────────
  const g = new dagre.graphlib.Graph({ multigraph: true });
  g.setGraph({
    rankdir: data.rankdir || "LR", // 依赖方向走横向：左=起点，右=它依赖的
    nodesep: data.nodesep ?? 18,
    ranksep: data.ranksep ?? 96,
    marginx: 16,
    marginy: 16,
  });
  g.setDefaultEdgeLabel(() => ({}));

  const byId = new Map();
  data.nodes.forEach((n) => {
    byId.set(n.id, n);
    g.setNode(n.id, { width: widthOf(n), height: NODE_H });
  });
  (data.edges || []).forEach((e, i) => {
    if (byId.has(e.source) && byId.has(e.target)) {
      g.setEdge(e.source, e.target, {}, "e" + i);
    }
  });

  dagre.layout(g); // acyclic 预处理在内部完成：有环也不会算乱

  // viewBox 必须按**实际 bbox**算，不能用 `0 0 width height`：
  // dagre 断环时会反转边，边的控制点可以落在负坐标，节点也不保证从 0 起。
  // 用 graph().width/height 当视口会把最左那一层推到视口外 —— 表现为
  // 「起点不见了，只有几条线从边缘伸进来」。
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  const bump = (x, y) => {
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  };
  g.nodes().forEach((id) => {
    const p = g.node(id);
    if (!p) return;
    bump(p.x - p.width / 2, p.y - p.height / 2);
    bump(p.x + p.width / 2, p.y + p.height / 2);
  });
  g.edges().forEach((e) => {
    const ed = g.edge(e);
    (ed && ed.points ? ed.points : []).forEach((pt) => bump(pt.x, pt.y));
  });
  if (!isFinite(minX)) { minX = 0; minY = 0; maxX = 1; maxY = 1; }

  const M = 18; // 留白，免得最外层节点贴边被裁
  const VX = minX - M;
  const VY = minY - M;
  const W = Math.max(maxX - minX + M * 2, 1);
  const H = Math.max(maxY - minY + M * 2, 1);

  // ── 画 ────────────────────────────────────────────────────────────────────
  const NS = "http://www.w3.org/2000/svg";
  const el = (t, a = {}) => {
    const e = document.createElementNS(NS, t);
    for (const k in a) e.setAttribute(k, a[k]);
    return e;
  };

  mount.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "gs-wrap";
  mount.appendChild(wrap);

  const svg = el("svg", {
    class: "gs-svg",
    viewBox: `${VX} ${VY} ${W} ${H}`,
    // 不做 fit-to-container 缩放：字号固定 13px，靠容器滚动浏览。
    // 「缩到装下」正是上一个组件让标签消失的原因，这里不重犯。
    width: W,
    height: H,
    role: "img",
    "aria-label": `依赖关系图，${data.nodes.length} 个节点，${(data.edges || []).length} 条依赖边`,
  });
  wrap.appendChild(svg);

  // 箭头：每种边色一个 marker，否则所有箭头只能同色
  const defs = el("defs");
  const edgeColors = new Set(
    (data.edges || []).map((e) => e.color || "#9aa4b2"),
  );
  edgeColors.forEach((c, i) => {
    const id = "gs-ar-" + btoa(c).replace(/[^a-zA-Z0-9]/g, "");
    const mk = el("marker", {
      id,
      viewBox: "0 0 10 10",
      refX: "9",
      refY: "5",
      markerWidth: "7",
      markerHeight: "7",
      orient: "auto-start-reverse",
    });
    mk.appendChild(el("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: c }));
    defs.appendChild(mk);
  });
  svg.appendChild(defs);

  const gEdges = el("g", { class: "gs-edges" });
  const gNodes = el("g", { class: "gs-nodes" });
  svg.appendChild(gEdges);
  svg.appendChild(gNodes);

  const arrowFor = (c) => "gs-ar-" + btoa(c).replace(/[^a-zA-Z0-9]/g, "");

  (data.edges || []).forEach((e, i) => {
    const ed = g.edge(e.source, e.target, "e" + i);
    if (!ed || !ed.points || ed.points.length < 2) return;
    const p = ed.points;
    // 贝塞尔平滑：dagre 给的是折线控制点，直连会有明显折角
    let d = `M ${p[0].x},${p[0].y}`;
    for (let k = 1; k < p.length - 1; k++) {
      const mx = (p[k].x + p[k + 1].x) / 2;
      const my = (p[k].y + p[k + 1].y) / 2;
      d += ` Q ${p[k].x},${p[k].y} ${mx},${my}`;
    }
    d += ` L ${p[p.length - 1].x},${p[p.length - 1].y}`;
    const color = e.color || "#9aa4b2";
    const path = el("path", {
      class: "gs-edge",
      d,
      stroke: color,
      "stroke-width": e.width || 1.6,
      "marker-end": `url(#${arrowFor(color)})`,
    });
    if (e.title) {
      const t = el("title");
      t.textContent = e.title;
      path.appendChild(t);
    }
    gEdges.appendChild(path);
  });

  let selectedId = (data._state && data._state.selected_node) || null;

  data.nodes.forEach((n) => {
    const p = g.node(n.id);
    if (!p) return;
    const w = p.width;
    const grp = el("g", {
      class: "gs-node" + (n.id === selectedId ? " is-selected" : ""),
      transform: `translate(${p.x - w / 2},${p.y - NODE_H / 2})`,
      tabindex: "0",
      role: "button",
      "aria-label": `${labelOf(n)}${n.type ? "，类型 " + n.type : ""}`,
    });

    grp.appendChild(
      el("rect", {
        class: "gs-box",
        width: w,
        height: NODE_H,
        rx: 6,
        ry: 6,
        fill: n.fill || "#ffffff",
        stroke: n.stroke || "#c8d1de",
      }),
    );
    // 左侧色条编码分组：比整块染色更容易读文字，也不必为深色背景改字色
    grp.appendChild(
      el("rect", {
        class: "gs-accent",
        width: 4,
        height: NODE_H,
        rx: 2,
        ry: 2,
        fill: n.accent || "#8C8C8C",
      }),
    );

    const txt = el("text", {
      class: "gs-label",
      x: PAD_X,
      y: NODE_H / 2 + 4.5,
    });
    txt.textContent = labelOf(n);
    grp.appendChild(txt);

    if (n.badge) {
      const bx = w - 10;
      grp.appendChild(
        el("circle", { class: "gs-badge", cx: bx, cy: 9, r: 6.5,
                       fill: n.badge_fill || "#e0544f" }),
      );
      const bt = el("text", { class: "gs-badge-t", x: bx, y: 12 });
      bt.textContent = n.badge;
      grp.appendChild(bt);
    }

    const tip = el("title");
    tip.textContent =
      labelOf(n) + (n.type ? "\n类型：" + n.type : "") +
      (n.group ? "\n分组：" + n.group : "") +
      (n.degree != null ? "\n度数：" + n.degree : "");
    grp.appendChild(tip);

    const select = () => {
      gNodes.querySelectorAll(".gs-node.is-selected")
        .forEach((x) => x.classList.remove("is-selected"));
      grp.classList.add("is-selected");
      selectedId = n.id;
      // 持久状态：Python 侧读 result.selected_node
      setStateValue("selected_node", n.id);
    };

    grp.addEventListener("click", select);
    grp.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        select();
      }
    });
    // 双击 = 展开邻居。用 trigger 而不是 state：它是一次性动作，
    // 用 state 会在下一次 rerun 里被当成「仍然要展开」而重复触发。
    grp.addEventListener("dblclick", (ev) => {
      ev.preventDefault();
      setTriggerValue("expand", n.id);
    });

    gNodes.appendChild(grp);
  });

  // 图比容器宽/高时，把**起点**滚进视野。不加这一步初始停在 scrollLeft=0，
  // 对 rankdir=LR 恰好是起点侧、看着没问题，但一旦布局把锚点排在中间
  // （有环被反转、或锚点既有上游又有下游时会这样），用户开局看到的就是
  // 图的中段，找不到自己选的那个服务。所以按锚点坐标显式对准。
  const anchorId = data.anchor;
  if (anchorId && byId.has(anchorId)) {
    const ap = g.node(anchorId);
    if (ap) {
      requestAnimationFrame(() => {
        wrap.scrollLeft = Math.max(0, ap.x - VX - wrap.clientWidth * 0.28);
        wrap.scrollTop = Math.max(0, ap.y - VY - wrap.clientHeight / 2);
      });
    }
  }
}
