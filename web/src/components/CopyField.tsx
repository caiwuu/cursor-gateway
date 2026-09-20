import { Button, Input, Space, App } from "antd";

export function CopyField({ value, compact = false }: { value: string; compact?: boolean }) {
  const { message } = App.useApp();
  return (
    <Space.Compact style={{ width: "100%" }}>
      <Input value={value} readOnly size={compact ? "small" : "middle"} />
      <Button
        size={compact ? "small" : "middle"}
        onClick={async () => {
          await navigator.clipboard.writeText(value);
          message.success("已复制");
        }}
      >
        复制
      </Button>
    </Space.Compact>
  );
}
