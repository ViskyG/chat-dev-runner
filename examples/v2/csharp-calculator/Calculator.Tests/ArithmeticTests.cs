using Microsoft.VisualStudio.TestTools.UnitTesting;
namespace Calculator.Tests;
[TestClass]
public class ArithmeticTests
{
    [DataTestMethod]
    [DataRow(7d, "+", 5d, 12d)] [DataRow(7d, "-", 5d, 2d)] [DataRow(7d, "*", 5d, 35d)] [DataRow(7d, "/", 2d, 3.5d)]
    public void Calculates(double left, string operation, double right, double expected) => Assert.AreEqual(expected, Arithmetic.Calculate(left, operation, right));
    [TestMethod] public void RejectsZeroDivision() => Assert.ThrowsException<ArgumentException>(() => Arithmetic.Calculate(1, "/", 0));
    [TestMethod] public void RejectsNonFinite() => Assert.ThrowsException<ArgumentException>(() => Arithmetic.Calculate(double.NaN, "+", 1));
}
