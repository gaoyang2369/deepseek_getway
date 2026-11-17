import {
  node,
  money,
  date,
  notice,
  confirmAction,
  api,
  button,
  badge,
  table,
  toolbar,
  field,
  yuan,
  stacked,
} from "./common.js";
import { openForm, closeModal } from "./forms.js";
let includePriceHistory = false;
export async function pricesView(cache, load) {
  const rows = await api(
    "/prices" + (includePriceHistory ? "?include_ended=true" : ""),
  );
  const source = node("a", "查看 DeepSeek 官方人民币价格");
  source.href = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/";
  source.target = "_blank";
  source.rel = "noopener noreferrer";
  return [
    node(
      "p",
      "官网导入采用高峰费率保守估算，空闲时段实际费用可能更低。来源与计价版本见“管理”，更新保留历史费用。",
      "section-note",
    ),
    source,
    toolbar(
      includePriceHistory ? "全部计价版本（含历史）" : "当前可用模型",
      button("从 DeepSeek 官网更新", async () => {
        const result = await api("/prices/import-official", "POST", {});
        await load();
        notice(
          "已从 DeepSeek 官网获取价格：更新 " +
            result.created.length +
            " 个模型，" +
            result.unchanged.length +
            " 个无需变更。按官方高峰价保守估算。",
          "success",
        );
      }),
      button(
        includePriceHistory ? "仅看当前模型" : "查看历史计价",
        async () => {
          includePriceHistory = !includePriceHistory;
          await load();
        },
      ),
      button("＋ 自定义价格", () =>
        openForm(
          "发布计价版本",
          [
            field("客户端模型名", "model"),
            field("DeepSeek 上游模型名", "upstream_model"),
            field("唯一版本号", "version"),
            field("缓存未命中：元 / 百万 Token", "input_miss", "number"),
            field("缓存命中：元 / 百万 Token", "input_hit", "number"),
            field("输出：元 / 百万 Token", "output", "number"),
            field("费用标记", "estimated", "text", "true", [
              ["true", "估算"],
              ["false", "经核验的固定精确价"],
            ]),
            field("来源、计价时段、币种换算及保守规则说明", "note", "textarea"),
          ],
          async (d) => {
            await api("/prices", "POST", {
              ...d,
              input_miss: yuan(d.input_miss),
              input_hit: yuan(d.input_hit),
              output: yuan(d.output),
              estimated: d.estimated === "true",
            });
            closeModal();
            await load();
          },
        ),
      ),
    ),
    table(
      [
        "模型",
        { label: "未命中输入", numeric: true },
        { label: "缓存命中", numeric: true },
        { label: "输出", numeric: true },
        { label: "状态", secondary: true },
        "操作",
      ],
      rows.map((r) => [
        r.model,
        money(r.input_miss).slice(1),
        money(r.input_hit).slice(1),
        money(r.output).slice(1),
        badge(
          r.ends_at && new Date(r.ends_at) <= new Date() ? "已结束" : "可用",
        ),
        button("管理", () =>
          openForm(
            "计价详情 · " + r.model,
            [
              node(
                "p",
                "单价单位：元／百万 Token。计价版本不等于上游账单核验记录。",
                "section-note",
              ),
              stacked("计价版本", r.version),
              stacked("上游模型", r.upstream_model),
              stacked("生效时间", date(r.starts_at)),
              stacked("结束时间", date(r.ends_at)),
              stacked(
                "费率标记",
                r.estimated ? "保守估算费率" : "固定费率（非账单核验）",
              ),
              stacked("来源与依据", r.note),
              ...(r.ends_at && new Date(r.ends_at) <= new Date()
                ? []
                : [
                    button(
                      "删除模型",
                      async () => {
                        if (
                          !(await confirmAction(
                            "删除模型“" + r.model + "”？",
                            "该计价版本停止供新请求使用；历史调用和账本保留。",
                          ))
                        )
                          return;
                        await api("/prices/" + r.id, "DELETE");
                        closeModal();
                        await load();
                        notice(
                          "模型已移出当前列表，历史计价和账本已保留。",
                          "success",
                        );
                      },
                      "danger",
                    ),
                  ]),
            ],
            null,
          ),
        ),
      ]),
      "compact-table price-table",
    ),
    node(
      "p",
      "单价单位：元／百万 Token。历史调用保留当时的计价版本。",
      "section-note",
    ),
  ];
}
