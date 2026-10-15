using System.Globalization;
namespace Calculator;
public static class Arithmetic
{
    public static double Calculate(double left, string operation, double right)
    {
        if (!double.IsFinite(left) || !double.IsFinite(right)) throw new ArgumentException("Operands must be finite numbers");
        return operation switch
        {
            "+" => left + right, "-" => left - right, "*" => left * right,
            "/" when right != 0 => left / right,
            "/" => throw new ArgumentException("Cannot divide by zero"),
            _ => throw new ArgumentException("Unknown operation")
        };
    }
    public static int Main(string[] args)
    {
        try
        {
            if (args.Length != 3) throw new ArgumentException("Usage: Calculator 12 / 4");
            Console.WriteLine(Calculate(double.Parse(args[0], CultureInfo.InvariantCulture), args[1], double.Parse(args[2], CultureInfo.InvariantCulture)).ToString(CultureInfo.InvariantCulture));
            return 0;
        }
        catch (Exception error) when (error is ArgumentException or FormatException or OverflowException)
        {
            Console.Error.WriteLine(error.Message);
            return 2;
        }
    }
}
