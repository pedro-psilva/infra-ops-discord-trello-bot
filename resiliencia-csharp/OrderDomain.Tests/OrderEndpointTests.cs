using System.Net;
using System.Text.Json;
using FluentAssertions;
using RestSharp;

namespace OrderDomain.Tests;

public class OrderEndpointTests
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true
    };

    [Fact(Skip = "Requer API em execucao em http://localhost:5000.")]
    public async Task PostOrder_Returns201_WithValidPayload()
    {
        // Arrange
        var client = new RestClient("http://localhost:5000");
        var request = new RestRequest("/api/orders", Method.Post);
        request.AddJsonBody(new { ProductId = 1, Quantity = 2 });

        // Act
        var response = await client.ExecuteAsync(request);

        // Assert
        response.StatusCode.Should().Be(HttpStatusCode.Created);
        response.Content.Should().NotBeNullOrWhiteSpace();

        var body = JsonSerializer.Deserialize<OrderResponse>(
            response.Content!,
            JsonOptions);

        body.Should().NotBeNull();
        body!.OrderId.Should().BeGreaterThan(0);
        body.ProductId.Should().Be(1);
        body.Quantity.Should().Be(2);
    }

    private sealed record OrderResponse(int OrderId, int ProductId, int Quantity);
}
