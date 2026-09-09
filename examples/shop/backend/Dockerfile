FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
ARG SERVICE=Catalog
WORKDIR /src
COPY examples/shop/backend/ ./
RUN dotnet publish ${SERVICE}/${SERVICE}.csproj -c Release -o /app
FROM mcr.microsoft.com/dotnet/aspnet:10.0
ARG SERVICE=Catalog
ENV SHOP_ASSEMBLY=${SERVICE}.dll ASPNETCORE_URLS=http://+:8080
WORKDIR /app
COPY --from=build /app ./
USER app
ENTRYPOINT ["sh","-c","exec dotnet \"$SHOP_ASSEMBLY\""]
