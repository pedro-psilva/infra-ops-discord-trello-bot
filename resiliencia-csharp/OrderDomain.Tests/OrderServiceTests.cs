using FluentAssertions;
using Moq;

namespace OrderDomain.Tests;

public class OrderServiceTests
{
    [Fact]
    public void CalculateTotal_WithDiscount_ReturnsCorrectValue()
    {
        // Arrange
        var mockRepo = new Mock<IOrderRepository>();
        var service = new OrderService(mockRepo.Object);

        // Act
        var result = service.CalculateTotal(100m, 0.10m);

        // Assert
        result.Should().Be(90m);
    }

    [Fact]
    public void CalculateTotal_WithNegativeDiscount_ThrowsArgumentException()
    {
        // Arrange
        var mockRepo = new Mock<IOrderRepository>();
        var service = new OrderService(mockRepo.Object);

        // Act
        Action act = () => service.CalculateTotal(100m, -0.10m);

        // Assert
        act.Should()
            .Throw<ArgumentException>()
            .WithMessage("Desconto inválido");
    }

    [Fact]
    public void CalculateTotal_WithEmptyOrder_ReturnsZero()
    {
        // Arrange
        var mockRepo = new Mock<IOrderRepository>();
        var service = new OrderService(mockRepo.Object);

        // Act
        var result = service.CalculateTotal(0m, 0m);

        // Assert
        result.Should().Be(0m);
    }
}
