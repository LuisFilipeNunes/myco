using System.Text.Json;
using System.Text;
using LibreHardwareMonitor.Hardware;

Console.OutputEncoding = new UTF8Encoding(false);
var computer = new Computer { IsCpuEnabled = true, IsGpuEnabled = true };
try
{
    computer.Open();
    while (Console.ReadLine() is not null)
    {
        float? cpu = null;
        float? gpu = null;
        foreach (var hardware in computer.Hardware)
            ReadTemperatures(hardware, hardware.HardwareType, ref cpu, ref gpu);

        Console.WriteLine(JsonSerializer.Serialize(new
        {
            cpu_temperature_c = cpu,
            gpu_temperature_c = gpu,
        }));
    }
    return 0;
}
catch (Exception error)
{
    // Keep stdout machine-readable, including initialization and driver failures.
    Console.WriteLine(JsonSerializer.Serialize(new { error = error.Message }));
    return 1;
}
finally
{
    computer.Close();
}

static void ReadTemperatures(IHardware hardware, HardwareType type, ref float? cpu, ref float? gpu)
{
    hardware.Update();
    foreach (var sensor in hardware.Sensors)
    {
        if (sensor.SensorType != SensorType.Temperature ||
            sensor.Value is not float value || !float.IsFinite(value) || value < 0)
            continue;

        if (type == HardwareType.Cpu)
            cpu = cpu is null ? value : Math.Max(cpu.Value, value);
        else if (type is HardwareType.GpuNvidia or HardwareType.GpuAmd or HardwareType.GpuIntel)
            gpu = gpu is null ? value : Math.Max(gpu.Value, value);
    }
    foreach (var child in hardware.SubHardware)
        ReadTemperatures(child, type, ref cpu, ref gpu);
}
