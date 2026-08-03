package com.example.quotes;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.reactive.function.server.RouterFunction;
import org.springframework.web.reactive.function.server.ServerResponse;

import static org.springframework.web.reactive.function.server.RequestPredicates.GET;
import static org.springframework.web.reactive.function.server.RequestPredicates.POST;
import static org.springframework.web.reactive.function.server.RouterFunctions.route;

@Configuration
public class WatchlistRouter {

    @Bean
    public RouterFunction<ServerResponse> watchlistRoutes(WatchlistHandler handler) {
        return route(GET("/watchlists"), handler::list)
                .andRoute(GET("/watchlists/{name}"), handler::get)
                .andRoute(POST("/watchlists"), handler::create);
    }
}
